"""
Idempotency layer for order execution.
Prevents duplicate orders during network retries.
Solves the "Two Generals Problem" in distributed systems.

Admission policy
----------------
Only SUCCESSFUL placements enter the deduplication cache.
Failed placements (broker reject, network error, etc.) must NOT be cached —
the caller may legitimately retry and the retry should go through.
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
    Ensures multiple identical requests result in single state change.
    Critical for preventing duplicate orders on timeout/retry scenarios.
    """

    def __init__(self, db_path: Optional[str] = "./data/idempotency.json", ttl: int = 3600, cache_ttl: Optional[int] = None):
        if db_path in (None, "", ":memory:"):
            self.db_path = None
        else:
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(exist_ok=True)
        self.cache_ttl = int(cache_ttl if cache_ttl is not None else ttl)
        self._cache: Dict[str, Dict[str, Any]] = self._load_cache()
        logger.info("idempotency_manager_initialized", ttl=self.cache_ttl)

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
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, indent=2)

    def generate_key(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        strategy: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> str:
        components = f"{market_id}:{outcome or ''}:{side}:{str(price)}:{str(size)}:{strategy or ''}"
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
                key,
                market_id,
                outcome,
                side,
            )
        return key

    def is_duplicate(self, key: str) -> bool:
        if key in self._cache:
            entry = self._cache[key]
            # Only entries marked as successful are considered duplicates.
            if not entry.get("success"):
                return False
            age = time.time() - entry.get("timestamp", 0)
            if age < self.cache_ttl:
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
                        key,
                        age,
                        entry.get("attempts", 0),
                    )
                return True

            if _structlog_available:
                logger.info("idempotency_cache_expired", key=key, age_seconds=age)
            else:
                logger.info("idempotency_cache_expired | key=%s age_seconds=%s", key, age)
            del self._cache[key]
            self._save_cache()
            return False

        return False

    def get_cached_result(self, key: str) -> Optional[Dict]:
        if key in self._cache:
            entry = self._cache[key]
            age = time.time() - entry.get("timestamp", 0)
            if age < self.cache_ttl:
                if _structlog_available:
                    logger.info("returning_cached_result", key=key, age_seconds=age)
                else:
                    logger.info("returning_cached_result | key=%s age_seconds=%s", key, age)
                return entry.get("result")
        return None

    def record(self, key: str, status: str = "pending") -> None:
        """Record a pending attempt.  Does NOT overwrite an existing success entry."""
        existing = self._cache.get(key, {})
        # Never downgrade a successful entry back to pending.
        if existing.get("success"):
            return
        attempts = existing.get("attempts", 0) + 1
        self._cache[key] = {
            "timestamp": time.time(),
            "status": status,
            "attempts": attempts,
            "result": existing.get("result"),
            "success": False,
            "order_id": existing.get("order_id"),
        }
        # Pending entries are not persisted to disk — only successes are.

    def update_result(self, key: str, result: Dict[str, Any]) -> None:
        """Update the cache with a broker result.  Only persists on success."""
        serialized = self._serialize_value(result)
        is_success = bool(serialized.get("success")) if isinstance(serialized, dict) else False
        existing = self._cache.get(key, {})
        self._cache[key] = {
            "timestamp": existing.get("timestamp", time.time()),
            "status": "success" if is_success else "failed",
            "attempts": existing.get("attempts", 1),
            "result": serialized,
            "success": is_success,
            "order_id": serialized.get("order_id") if isinstance(serialized, dict) else None,
        }
        if is_success:
            self._save_cache()
        else:
            # Failed results: remove from cache so caller can retry freely.
            del self._cache[key]

    def record_placement(self, key: str, result: Dict[str, Any]) -> None:
        """
        Record the final result of a placement attempt.

        This is the canonical method callers should use after receiving a
        broker response.  Behaviour:
        - result["success"] is True  => enters the dedup cache; persisted to disk.
        - result["success"] is False => NOT cached; the next attempt is allowed.

        Calling record_placement multiple times for the same key increments the
        attempts counter on success entries.
        """
        serialized = self._serialize_value(result)
        is_success = bool(serialized.get("success")) if isinstance(serialized, dict) else False
        if not is_success:
            # Don't cache failed placements.
            return
        existing = self._cache.get(key, {})
        attempts = existing.get("attempts", 0) + 1
        self._cache[key] = {
            "timestamp": time.time(),
            "status": "success",
            "attempts": attempts,
            "result": serialized,
            "success": True,
            "order_id": serialized.get("order_id") if isinstance(serialized, dict) else None,
        }
        self._save_cache()

    def check_duplicate(self, key: str) -> Optional[Dict]:
        if self.is_duplicate(key):
            return self.get_cached_result(key)
        return None

    def _serialize_value(self, value: Any):
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

    def record_attempt(self, key: str, result: Dict) -> None:
        existing = self._cache.get(key, {})
        if not existing:
            self.record(key, status="pending")
        self.update_result(key, result)
        attempts = self._cache.get(key, {}).get("attempts", 1)
        if _structlog_available:
            logger.info("order_attempt_recorded", key=key, attempts=attempts)
        else:
            logger.info("order_attempt_recorded | key=%s attempts=%s", key, attempts)

    def record_order(self, key: str, result: Dict) -> None:
        self.record_attempt(key, result)

    def clear_expired(self) -> None:
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
