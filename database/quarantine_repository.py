from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional


class QuarantineRepository:
    """SQLite-backed repository for market quarantine state."""

    def __init__(self, ledger) -> None:
        self.ledger = ledger

    async def load_active_entries(self) -> Dict[str, Dict[str, Any]]:
        rows = await self.ledger.execute(
            """
            SELECT market_id,
                   reason,
                   source,
                     status,
                   added_at,
                   evidence_sample_size,
                   rolling_win_rate,
                   rolling_pnl,
                   review_at,
                   expiry_at,
                   notes,
                   disabled_by_config
            FROM market_quarantine
            WHERE COALESCE(disabled_by_config, 0) = 0
            ORDER BY market_id ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        return {
            str(row.get("market_id")): dict(row)
            for row in rows
            if row.get("market_id") is not None
        }

    async def seed_runtime_blocklist(
        self,
        blocked_markets: Iterable[str],
        *,
        auto_review_after_days: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        added = 0
        for market_id in sorted(str(market_id) for market_id in blocked_markets):
            before = await self.ledger.execute_scalar(
                "SELECT COUNT(*) FROM market_quarantine WHERE market_id = ?",
                (market_id,),
            )
            if before:
                continue

            review_at = (now + timedelta(days=auto_review_after_days)).isoformat()
            await self.ledger.execute(
                """
                INSERT INTO market_quarantine (
                    market_id, reason, source, status, added_at,
                    evidence_sample_size, rolling_win_rate, rolling_pnl,
                    review_at, expiry_at, notes, disabled_by_config
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id,
                    "seeded_from_runtime_controls",
                    "runtime_controls.blocked_markets",
                    "pending_review",
                    now.isoformat(),
                    0,
                    None,
                    None,
                    review_at,
                    None,
                    "seeded from runtime_controls.blocked_markets",
                    0,
                ),
                commit=True,
            )
            added += 1
        return added

    def get_active_entry(
        self,
        cache: Dict[str, Dict[str, Any]],
        market_id: str,
    ) -> Optional[Dict[str, Any]]:
        entry = dict(cache.get(str(market_id)) or {})
        if not entry:
            return None
        if int(entry.get("disabled_by_config") or 0):
            return None

        status = str(entry.get("status") or "pending_review").lower()
        if status in {"approved", "disabled", "released"}:
            return None

        review_at = entry.get("review_at")
        if review_at:
            try:
                review_dt = datetime.fromisoformat(str(review_at).replace("Z", "+00:00"))
                if review_dt.tzinfo is None:
                    review_dt = review_dt.replace(tzinfo=timezone.utc)
                if review_dt <= datetime.now(timezone.utc) and status == "pending_review":
                    entry["status"] = "needs_review"
            except Exception:
                pass

        expiry_at = entry.get("expiry_at")
        if expiry_at:
            try:
                expiry_dt = datetime.fromisoformat(str(expiry_at).replace("Z", "+00:00"))
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                if expiry_dt <= datetime.now(timezone.utc):
                    return None
            except Exception:
                pass

        return entry

    @staticmethod
    def to_block_log_context(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reason": entry.get("reason"),
            "status": entry.get("status"),
            "source": entry.get("source"),
            "evidence_sample_size": entry.get("evidence_sample_size"),
            "rolling_win_rate": entry.get("rolling_win_rate"),
            "rolling_pnl": entry.get("rolling_pnl"),
            "notes": entry.get("notes"),
        }

    async def get_row(self, market_id: str) -> Optional[Dict[str, Any]]:
        row = await self.ledger.execute(
            """
            SELECT market_id,
                   reason,
                   source,
                     status,
                   added_at,
                   evidence_sample_size,
                   rolling_win_rate,
                   rolling_pnl,
                   review_at,
                   expiry_at,
                   notes,
                   disabled_by_config
            FROM market_quarantine
            WHERE market_id = ?
            """,
            (market_id,),
            fetch_one=True,
            as_dict=True,
        )
        return dict(row) if row else None
