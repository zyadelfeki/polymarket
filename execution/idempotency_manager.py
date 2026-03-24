"""
Idempotency layer for order execution.
Prevents duplicate orders during network retries.
Solves the "Two Generals Problem" in distributed systems.

Cache admission policy
----------------------
Only SUCCESSFUL placements are written to the persistent cache.  Locally-
rejected orders (circuit breaker blocked, insufficient balance, bet below
minimum, validation error) must NOT be cached — they are transient conditions
that may clear on the very next scan cycle.  Caching them as 'failed' would
block re-entry on a valid market for the entire TTL (default 1 hour).

The public API for callers is:
  1. is_duplicate(key)       — check before attempting placement
  2. record_placement(key, result)  — call ONLY after confirmed success
  3. check_duplicate(key)    — combined check+fetch for legacy callers
"""

import hashlib
import json
import time
from typing import Optional, Dict, Any
from pathlib import Path
from decimal import Decimal, getcontext
from enum import Enum
from dataclasses import is_dataclass, asdict
from datetime import datetime

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

getcontext().prec = 18


class IdempotencyManager:
    """
    Ensures multiple identical requests result in a single state change.
    Critical for preventing duplicate orders on timeout/retry scenarios.

    Admission invariant
    -------------------
    Only successful order placements are persisted.  Rejected/failed results
    are NEVER written to the cache or disk so transient blocks do not
    permanently suppress re-entry on a good market.
    """

    def __init__(
        self,
        db_path: Optional[str] = "./data/idempotency.json",
        ttl: int = 3600,
        cache_ttl: Optional[int] = None,
    ):
        if db_path in (None, "", ":memory:"):
            self.db_path = None
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(exist_ok=True)
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else ttl)
        self._cache: Dict[str, Dict[str, Any]] = self._load_cache()
        if _structlog_available:
            logger.info("idempotency_manager_initialized", ttl=self.cache_ttl)
        else:
            logger.info("idempotency_manager_initialized | ttl=%s", self.cache_ttl)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_cache(self) -> Dict:
        if self.db_path and self.db_path.exists():
            try:
                with open(self.db_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self) -> None:
        if not self.db_path:
            return
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
        except Exception as exc:
            if _structlog_available:
                logger.warning("idempotency_cache_save_failed", error=str(exc))
            else:
                logger.warning("idempotency_cache_save_failed | error=%s", exc)

    # ------------------------------------------------------------------
    # Key generation
    # ------------------------------------------------------------------

    def generate_key(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        strategy: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> str:
        components = (
            f"{market_id}:{outcome or ''}:{side}:{str(price)}:{str(size)}:{strategy or ''}"
        )
        full_hash = hashlib.sha256(components.encode()).hexdigest()
        key = full_hash[:16]
        if _structlog_available:
            logger.debug(
                "idempotency_key_generated",
                key=key,
                market=market_id,
                outcome=outcome,
                side=side,
            )
        else:
            logger.debug(
                "idempotency_key_generated | key=%s market=%s outcome=%s side=%s",
                key, market_id, outcome, side,
            )
        return key

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    def is_duplicate(self, key: str) -> bool:
        """
        Return True only if a SUCCESSFUL placement for this key exists in
        cache and has not expired.  Expired entries are evicted immediately.
        """
        if key not in self._cache:
            return False

        entry = self._cache[key]
        age = time.time() - entry.get("timestamp", 0)

        if age >= self.cache_ttl:
            del self._cache[key]
            self._save_cache()
            if _structlog_available:
                logger.info("idempotency_cache_expired", key=key, age_seconds=age)
            else:
                logger.info("idempotency_cache_expired | key=%s age_seconds=%s", key, age)
            return False

        # Only treat as duplicate when the cached entry was a success.
        # A failed/pending entry (should never exist with the new admission
        # policy, but guard defensively) must not block re-attempts.
        if not entry.get("success", False):
            return False

        if _structlog_available:
            logger.warning(
                "duplicate_order_detected",
                key=key,
                age_seconds=age,
                attempts=entry.get("attempts", 0),
            )
        else:
            logger.warning(
                "duplicate_order_detected | key=%s age_seconds=%s attempts=%s",
                key, age, entry.get("attempts", 0),
            )
        return True

    def get_cached_result(self, key: str) -> Optional[Dict]:
        """Return cached result payload if key is valid and unexpired."""
        if key not in self._cache:
            return None
        entry = self._cache[key]
        age = time.time() - entry.get("timestamp", 0)
        if age < self.cache_ttl:
            if _structlog_available:
                logger.info("returning_cached_result", key=key, age_seconds=age)
            else:
                logger.info("returning_cached_result | key=%s age_seconds=%s", key, age)
            return entry.get("result")
        return None

    def check_duplicate(self, key: str) -> Optional[Dict]:
        """Combined check: return cached result if duplicate, else None."""
        if self.is_duplicate(key):
            return self.get_cached_result(key)
        return None

    # ------------------------------------------------------------------
    # Write path  (success-only admission)
    # ------------------------------------------------------------------

    def record_placement(
        self,
        key: str,
        result: Dict[str, Any],
        *,
        order_id: Optional[str] = None,
    ) -> None:
        """
        Record a CONFIRMED SUCCESSFUL placement.  This is the ONLY method
        that writes to the persistent cache.  Must not be called for failed
        or locally-rejected orders.

        Increments the attempt counter correctly on every call (fixes the
        prior bug where update_result() always reset the counter to 1).
        """
        serialized = self._serialize_value(result)
        existing = self._cache.get(key, {})
        attempts = existing.get("attempts", 0) + 1
        placed_at = existing.get("timestamp") or time.time()

        self._cache[key] = {
            "timestamp": placed_at,        # preserve original placement time
            "last_seen": time.time(),       # updated on each re-confirmation
            "status": "success",
            "success": True,
            "attempts": attempts,
            "result": serialized,
            "order_id": (
                order_id
                or (serialized.get("order_id") if isinstance(serialized, dict) else None)
                or existing.get("order_id")
            ),
        }
        self._save_cache()

        if _structlog_available:
            logger.info(
                "order_placement_recorded",
                key=key,
                attempts=attempts,
                order_id=self._cache[key]["order_id"],
            )
        else:
            logger.info(
                "order_placement_recorded | key=%s attempts=%s order_id=%s",
                key, attempts, self._cache[key]["order_id"],
            )

    # ------------------------------------------------------------------
    # Legacy compatibility shims
    # (kept so existing callers in execution_service_v2 don't break)
    # ------------------------------------------------------------------

    def record(self, key: str, status: str = "pending") -> None:
        """
        Legacy: mark a key as in-flight (pending).  Does NOT persist to disk
        because pending entries must not block re-attempts after a restart.
        """
        existing = self._cache.get(key, {})
        # Do not overwrite a successful entry with a pending one.
        if existing.get("success"):
            return
        self._cache[key] = {
            "timestamp": existing.get("timestamp") or time.time(),
            "status": status,
            "attempts": existing.get("attempts", 0) + 1,
            "result": existing.get("result"),
            "success": False,
            "order_id": existing.get("order_id"),
        }
        # Intentionally NOT calling _save_cache() — pending must not persist.

    def update_result(self, key: str, result: Dict[str, Any]) -> None:
        """
        Legacy shim.  Routes success=True results through record_placement()
        and silently drops failure results (admission policy).
        """
        serialized = self._serialize_value(result)
        if isinstance(serialized, dict) and serialized.get("success"):
            self.record_placement(key, result)
        else:
            # Failed/rejected outcomes are NOT written to cache.
            # Remove any stale pending entry for this key so the next scan
            # cycle can retry without hitting a false duplicate.
            self._cache.pop(key, None)

    def record_attempt(self, key: str, result: Dict) -> None:
        """Legacy shim — delegates to update_result."""
        self.update_result(key, result)

    def record_order(self, key: str, result: Dict) -> None:
        """Legacy shim — delegates to update_result."""
        self.update_result(key, result)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear_expired(self) -> None:
        """Evict all entries whose TTL has elapsed."""
        now = time.time()
        expired = [
            k for k, v in self._cache.items()
            if now - v.get("timestamp", 0) >= self.cache_ttl
        ]
        for k in expired:
            del self._cache[k]
        if expired:
            if _structlog_available:
                logger.info("cache_cleanup_completed", entries_removed=len(expired))
            else:
                logger.info("cache_cleanup_completed | entries_removed=%s", len(expired))
            self._save_cache()

    def get_stats(self) -> Dict[str, Any]:
        total_entries = len(self._cache)
        successful_orders = sum(1 for v in self._cache.values() if v.get("success"))
        return {
            "total_cached": total_entries,
            "successful": successful_orders,
            "failed": total_entries - successful_orders,
            "cache_ttl": self.cache_ttl,
        }

    # ------------------------------------------------------------------
    # Serialisation helper
    # ------------------------------------------------------------------

    def _serialize_value(self, value: Any) -> Any:
        if is_dataclass(value):
            return self._serialize_value(asdict(value))
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._serialize_value(v) for v in value]
        return value
