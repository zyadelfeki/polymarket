"""
DoNotTradeRegistry — persistent blocklist of markets/tokens that must not
be traded.

Motivations for blocking a market:
  * Chain-level errors (bad token IDs, duplicate/phantom markets).
  * Execution failures above a configurable threshold (e.g. 3 consecutive
    order-rejected responses from the exchange).
  * Manual overrides added by the operator (via ``block()`` at runtime or by
    editing the JSON file).
  * Markets whose Charlie signal was incorrect too many times in a row
    (future: wired from PerformanceTracker feedback).

Persistence strategy
--------------------
Backed by a lightweight JSON file so the blocklist survives restarts without
requiring a database.  Writes are atomic (write-to-tmp, rename) to prevent
corruption on crash.

Thread-safety
-------------
All mutating operations hold ``_lock`` (an asyncio.Lock).  Read-path
(``is_blocked``) is lock-free because Python dict reads are thread-safe
for GIL-guarded CPython and we only need eventual consistency for reads.

Usage::

    registry = DoNotTradeRegistry(path="data/do_not_trade.json", auto_load=True)

    # Block a market
    registry.block("0xabc...", reason="3 consecutive exchange rejections")

    # Check before placing an order
    if registry.is_blocked(market_id):
        return

    # Clear a manual override once the root cause is fixed
    registry.unblock("0xabc...")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "data/do_not_trade.json"


class DoNotTradeRegistry:
    """
    In-memory blocklist with optional JSON persistence.

    Parameters
    ----------
    path:
        Path to the JSON backing file.  Created on first ``block()`` call
        if it does not exist.  Pass ``None`` to run in memory-only mode.
    auto_load:
        If True (default), load existing entries from ``path`` on construction.
    max_auto_blocks:
        Upper bound on automatically escalated market blocks.  Prevents a
        runaway cascade from blocking the whole market universe.
    """

    def __init__(
        self,
        path: Optional[str] = _DEFAULT_PATH,
        auto_load: bool = True,
        max_auto_blocks: int = 20,
    ) -> None:
        self._path = Path(path) if path else None
        self._max_auto_blocks = max_auto_blocks
        self._lock = asyncio.Lock()

        # {market_id → {"reason": str, "blocked_at": float, "auto": bool}}
        self._entries: Dict[str, Dict] = {}

        if auto_load and self._path and self._path.exists():
            self._load_from_disk()

    # ------------------------------------------------------------------ public

    def is_blocked(self, market_id: str) -> bool:
        """Return True if this market is on the blocklist."""
        return market_id in self._entries

    def block(
        self,
        market_id: str,
        reason: str = "",
        auto: bool = False,
    ) -> None:
        """
        Add ``market_id`` to the blocklist and persist.

        Parameters
        ----------
        market_id:
            The Polymarket condition_id or market slug to block.
        reason:
            Human-readable justification stored alongside the entry.
        auto:
            If True, this block was triggered automatically (e.g. consecutive
            failure threshold); manual blocks use False.
        """
        if auto:
            auto_count = sum(1 for e in self._entries.values() if e.get("auto"))
            if auto_count >= self._max_auto_blocks:
                logger.warning(
                    "do_not_trade_auto_block_cap_reached",
                    market_id=market_id,
                    cap=self._max_auto_blocks,
                    reason=reason,
                )
                return

        self._entries[market_id] = {
            "reason": reason,
            "blocked_at": time.time(),
            "auto": auto,
        }
        logger.info(
            "market_blocked",
            market_id=market_id,
            reason=reason,
            auto=auto,
            total_blocked=len(self._entries),
        )
        self._persist()

    def unblock(self, market_id: str) -> bool:
        """
        Remove ``market_id`` from the blocklist.

        Returns True if the market was present and removed, False if it was
        not on the list.
        """
        if market_id not in self._entries:
            return False
        del self._entries[market_id]
        logger.info("market_unblocked", market_id=market_id)
        self._persist()
        return True

    def all_blocked(self) -> Dict[str, Dict]:
        """Return a snapshot of all current entries (read-only copy)."""
        return dict(self._entries)

    def count(self) -> int:
        """Number of blocked markets."""
        return len(self._entries)

    # ------------------------------------------------------------------ internal

    def _load_from_disk(self) -> None:
        """Load entries from JSON file.  Silently ignores parse errors."""
        try:
            text = self._path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                self._entries = data
                logger.info(
                    "do_not_trade_registry_loaded",
                    path=str(self._path),
                    count=len(self._entries),
                )
        except Exception as exc:
            logger.warning(
                "do_not_trade_load_failed",
                path=str(self._path),
                error=str(exc),
            )

    def _persist(self) -> None:
        """Atomic write of current entries to disk.  No-op if path is None."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=".dnt_",
                suffix=".tmp",
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                    json.dump(self._entries, fp, indent=2)
                os.replace(tmp_path, self._path)  # atomic on POSIX and Win32
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception as exc:
            logger.warning(
                "do_not_trade_persist_failed",
                path=str(self._path),
                error=str(exc),
            )
