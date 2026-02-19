"""
Persistent Order Ledger — survives process restarts.

Single source of truth for every order the bot has ever attempted.
Backed by SQLite (no extra dependencies, safe for single-writer use).

Schema
------
orders
    id              INTEGER PK AUTOINCREMENT
    order_id        TEXT NOT NULL UNIQUE  — exchange-assigned ID
    market_id       TEXT NOT NULL
    token_id        TEXT NOT NULL
    outcome         TEXT NOT NULL         — "YES" | "NO"
    side            TEXT NOT NULL         — "BUY" (we only ever BUY outcomes)
    size            TEXT NOT NULL         — USDC amount, Decimal-serialised
    price           TEXT NOT NULL         — entry price, Decimal-serialised
    state           TEXT NOT NULL         — see OrderState below
    opened_at       TEXT NOT NULL         — ISO-8601 UTC
    closed_at       TEXT                  — ISO-8601 UTC or NULL
    pnl             TEXT                  — Decimal-serialised or NULL
    charlie_p_win   TEXT                  — Decimal-serialised or NULL
    charlie_conf    TEXT                  — Decimal-serialised or NULL
    charlie_regime  TEXT                  — "BULLISH"|"BEARISH"|"NEUTRAL" or NULL
    strategy        TEXT                  — e.g. "latency_arbitrage_btc"
    notes           TEXT                  — free-text, debug payload

Design decisions
----------------
* Decimal values are stored as TEXT to avoid IEEE-754 rounding.
* All writes go through ``upsert_order`` which is idempotent on order_id.
* ``reconcile_open_orders`` is called at startup and updates any order
  whose exchange state has changed while the process was dead.
* Thread-safety: every write acquires an asyncio.Lock because SQLite
  drivers are not async-safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_DEFAULT_PATH = os.getenv("ORDER_LEDGER_PATH", "data/orders_ledger.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT    NOT NULL UNIQUE,
    market_id      TEXT    NOT NULL,
    token_id       TEXT    NOT NULL DEFAULT '',
    outcome        TEXT    NOT NULL,
    side           TEXT    NOT NULL DEFAULT 'BUY',
    size           TEXT    NOT NULL,
    price          TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    opened_at      TEXT    NOT NULL,
    closed_at      TEXT,
    pnl            TEXT,
    charlie_p_win  TEXT,
    charlie_conf   TEXT,
    charlie_regime TEXT,
    strategy       TEXT,
    notes          TEXT
);
"""

_CREATE_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_orders_state     ON orders(state);",
    "CREATE INDEX IF NOT EXISTS idx_orders_market    ON orders(market_id);",
    "CREATE INDEX IF NOT EXISTS idx_orders_opened_at ON orders(opened_at);",
]


class OrderState(str, Enum):
    """Lifecycle states for a Polymarket prediction-market order."""
    CREATED          = "CREATED"           # row inserted before API call returns
    SUBMITTED        = "SUBMITTED"         # API accepted the order
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # some quantity matched
    FILLED           = "FILLED"            # fully matched
    CANCELLED        = "CANCELLED"         # cancelled before fill
    EXPIRED          = "EXPIRED"           # TTL elapsed
    SETTLED          = "SETTLED"           # market resolved; PnL computed
    ERROR            = "ERROR"             # API rejected / unknown state


# States from which a position can still evolve
OPEN_STATES = {
    OrderState.CREATED,
    OrderState.SUBMITTED,
    OrderState.PARTIALLY_FILLED,
    OrderState.FILLED,
}

# Terminal states — no further transitions expected
TERMINAL_STATES = {
    OrderState.CANCELLED,
    OrderState.EXPIRED,
    OrderState.SETTLED,
    OrderState.ERROR,
}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class OrderStore:
    """
    Async wrapper around a SQLite database that tracks every order.

    Usage::

        store = OrderStore()
        await store.initialize()
        await store.upsert_order(order_id="abc", market_id="0x...", ...)
        summary = await store.reconcile_open_orders(api_client)
    """

    def __init__(self, db_path: str = DB_DEFAULT_PATH) -> None:
        self._db_path = str(db_path)
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ setup

    async def initialize(self) -> None:
        """Create the DB file and schema if they don't exist yet."""
        async with self._lock:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = self._get_connection()
            conn.execute(_CREATE_TABLE)
            for idx_sql in _CREATE_INDICES:
                conn.execute(idx_sql)
            conn.commit()
            logger.info("order_store_initialized", path=self._db_path)

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                isolation_level=None,  # autocommit — we manage transactions manually
            )
            self._conn.row_factory = sqlite3.Row
            # WAL mode: readers don't block writers
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        return self._conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------ writes

    async def upsert_order(
        self,
        *,
        order_id: str,
        market_id: str,
        token_id: str = "",
        outcome: str,
        side: str = "BUY",
        size: Decimal,
        price: Decimal,
        state: OrderState,
        opened_at: Optional[datetime] = None,
        closed_at: Optional[datetime] = None,
        pnl: Optional[Decimal] = None,
        charlie_p_win: Optional[Decimal] = None,
        charlie_conf: Optional[Decimal] = None,
        charlie_regime: Optional[str] = None,
        strategy: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Insert a new order row or update an existing one (keyed on order_id).

        Idempotent: calling this twice with the same order_id and state is safe.
        """
        now_utc = datetime.now(timezone.utc).isoformat()
        opened_str = (opened_at or datetime.now(timezone.utc)).replace(tzinfo=timezone.utc).isoformat()
        closed_str = closed_at.replace(tzinfo=timezone.utc).isoformat() if closed_at else None

        async with self._lock:
            conn = self._get_connection()
            conn.execute("BEGIN;")
            try:
                conn.execute(
                    """
                    INSERT INTO orders
                        (order_id, market_id, token_id, outcome, side, size, price,
                         state, opened_at, closed_at, pnl,
                         charlie_p_win, charlie_conf, charlie_regime, strategy, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        state        = excluded.state,
                        closed_at    = COALESCE(excluded.closed_at, orders.closed_at),
                        pnl          = COALESCE(excluded.pnl, orders.pnl),
                        notes        = COALESCE(excluded.notes, orders.notes)
                    """,
                    (
                        order_id,
                        market_id,
                        token_id,
                        outcome.upper(),
                        side.upper(),
                        str(size),
                        str(price),
                        state.value,
                        opened_str,
                        closed_str,
                        str(pnl) if pnl is not None else None,
                        str(charlie_p_win) if charlie_p_win is not None else None,
                        str(charlie_conf) if charlie_conf is not None else None,
                        charlie_regime,
                        strategy,
                        notes,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    async def transition_state(
        self,
        order_id: str,
        new_state: OrderState,
        *,
        pnl: Optional[Decimal] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Fast-path state update — does not change any other column."""
        closed_at = (
            datetime.now(timezone.utc).isoformat()
            if new_state in TERMINAL_STATES
            else None
        )
        async with self._lock:
            conn = self._get_connection()
            conn.execute("BEGIN;")
            try:
                conn.execute(
                    """
                    UPDATE orders
                    SET state     = ?,
                        pnl       = COALESCE(?, pnl),
                        closed_at = COALESCE(?, closed_at),
                        notes     = COALESCE(?, notes)
                    WHERE order_id = ?
                    """,
                    (
                        new_state.value,
                        str(pnl) if pnl is not None else None,
                        closed_at,
                        notes,
                        order_id,
                    ),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    # ------------------------------------------------------------------ reads

    async def get_open_orders(self) -> List[Dict]:
        """Return all rows in OPEN_STATES as plain dicts."""
        placeholders = ",".join("?" * len(OPEN_STATES))
        states = [s.value for s in OPEN_STATES]
        async with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY opened_at ASC",
                states,
            )
            return [dict(row) for row in cur.fetchall()]

    async def get_all_orders(self, limit: int = 500) -> List[Dict]:
        async with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT * FROM orders ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    async def get_settled_pnl(self) -> Decimal:
        """Sum of PnL for all SETTLED orders."""
        async with self._lock:
            conn = self._get_connection()
            cur = conn.execute(
                "SELECT SUM(CAST(pnl AS REAL)) FROM orders WHERE state = 'SETTLED' AND pnl IS NOT NULL"
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return Decimal(str(row[0]))
            return Decimal("0")

    async def get_stats(self) -> Dict:
        """Aggregate stats for logging and circuit-breaker integration."""
        async with self._lock:
            conn = self._get_connection()
            cur = conn.execute("""
                SELECT
                    COUNT(*)                                          AS total,
                    SUM(CASE WHEN state IN ('CREATED','SUBMITTED','PARTIALLY_FILLED','FILLED')
                             THEN 1 ELSE 0 END)                      AS open_count,
                    SUM(CASE WHEN state = 'SETTLED' THEN 1 ELSE 0 END) AS settled_count,
                    SUM(CASE WHEN state = 'SETTLED' AND CAST(pnl AS REAL) > 0
                             THEN 1 ELSE 0 END)                      AS wins,
                    SUM(CASE WHEN state = 'SETTLED' AND CAST(pnl AS REAL) < 0
                             THEN 1 ELSE 0 END)                      AS losses,
                    SUM(CASE WHEN state = 'SETTLED' THEN CAST(pnl AS REAL) ELSE 0 END) AS total_pnl,
                    SUM(CASE WHEN state NOT IN ('CANCELLED','EXPIRED','ERROR')
                             THEN CAST(size AS REAL) ELSE 0 END)     AS total_exposure
                FROM orders
            """)
            row = cur.fetchone()
            if row:
                settled = row["settled_count"] or 0
                wins = row["wins"] or 0
                hit_rate = (wins / settled) if settled > 0 else None
                return {
                    "total_orders": row["total"] or 0,
                    "open_orders": row["open_count"] or 0,
                    "settled_orders": settled,
                    "wins": wins,
                    "losses": row["losses"] or 0,
                    "hit_rate": hit_rate,
                    "total_pnl": Decimal(str(row["total_pnl"] or 0)),
                    "total_exposure": Decimal(str(row["total_exposure"] or 0)),
                }
            return {}

    # ------------------------------------------------------------------ reconcile

    async def reconcile_open_orders(self, api_client) -> Dict:
        """
        On startup: compare stored open orders against current exchange state.

        For each open order:
          1. Query the exchange for its current status.
          2. If it has transitioned (filled / cancelled / expired), update the row.
          3. If the underlying market is resolved, compute PnL from final payout
             and mark SETTLED.

        Returns a summary dict printed to stdout by ``main.py``.
        """
        open_orders = await self.get_open_orders()
        if not open_orders:
            logger.info("reconcile_open_orders_none")
            return {
                "open_orders": 0,
                "resolved_while_offline": 0,
                "still_open": 0,
                "recovered_pnl": Decimal("0"),
            }

        logger.info("reconcile_start", open_order_count=len(open_orders))

        resolved_count = 0
        still_open_count = 0
        recovered_pnl = Decimal("0")

        for row in open_orders:
            order_id = row["order_id"]
            market_id = row["market_id"]
            size = Decimal(row["size"])
            price = Decimal(row["price"])

            # --- query current order status from exchange ---
            exchange_state: Optional[str] = None
            fill_price: Optional[Decimal] = None
            market_resolved = False
            winning_side: Optional[str] = None
            payout_per_share: Optional[Decimal] = None

            try:
                if hasattr(api_client, "get_order_status"):
                    status = await asyncio.wait_for(
                        api_client.get_order_status(order_id), timeout=10.0
                    )
                    if status:
                        exchange_state = (status.get("status") or "").upper()
                        raw_fill = status.get("avg_fill_price") or status.get("price")
                        if raw_fill is not None:
                            fill_price = Decimal(str(raw_fill))
            except asyncio.TimeoutError:
                logger.warning("reconcile_order_status_timeout", order_id=order_id)
            except Exception as exc:
                logger.warning(
                    "reconcile_order_status_error", order_id=order_id, error=str(exc)
                )

            try:
                if hasattr(api_client, "get_market"):
                    market = await asyncio.wait_for(
                        api_client.get_market(market_id), timeout=10.0
                    )
                    if market:
                        market_resolved = bool(market.get("closed") or market.get("resolved"))
                        winning_side = market.get("winning_side") or market.get("outcome")
                        raw_payout = market.get("payout_numerator") or market.get(
                            "payout_per_share"
                        )
                        if raw_payout is not None:
                            payout_per_share = Decimal(str(raw_payout))
            except asyncio.TimeoutError:
                logger.warning("reconcile_market_status_timeout", market_id=market_id)
            except Exception as exc:
                logger.warning(
                    "reconcile_market_status_error", market_id=market_id, error=str(exc)
                )

            # --- decide transition ---
            if market_resolved and payout_per_share is not None:
                quantity = size / price if price > 0 else Decimal("0")
                pnl = quantity * payout_per_share - size
                await self.transition_state(
                    order_id,
                    OrderState.SETTLED,
                    pnl=pnl,
                    notes=f"resolved_offline winning_side={winning_side} payout={payout_per_share}",
                )
                recovered_pnl += pnl
                resolved_count += 1
                logger.info(
                    "order_settled_offline",
                    order_id=order_id,
                    market_id=market_id,
                    pnl=str(pnl),
                    winning_side=winning_side,
                )

            elif exchange_state in {"CANCELLED", "EXPIRED"}:
                new_state = (
                    OrderState.CANCELLED
                    if exchange_state == "CANCELLED"
                    else OrderState.EXPIRED
                )
                await self.transition_state(order_id, new_state, notes="resolved_offline")
                resolved_count += 1
                logger.info(
                    "order_closed_offline",
                    order_id=order_id,
                    exchange_state=exchange_state,
                )

            else:
                still_open_count += 1

        summary = {
            "open_orders": len(open_orders),
            "resolved_while_offline": resolved_count,
            "still_open": still_open_count,
            "recovered_pnl": recovered_pnl,
        }

        logger.info("reconcile_complete", **{k: str(v) for k, v in summary.items()})
        return summary

    # ------------------------------------------------------------------ snapshot

    async def shutdown_snapshot(self, price_feed=None) -> Dict:
        """
        Log a final human-readable snapshot of open exposure on shutdown.

        Called by ``main.py`` inside ``stop()`` before closing the DB.
        """
        open_orders = await self.get_open_orders()
        stats = await self.get_stats()

        total_exposure = Decimal("0")
        mark_pnl = Decimal("0")

        for row in open_orders:
            size = Decimal(row["size"])
            total_exposure += size

            # Attempt a mark-to-market using live price if available
            if price_feed is not None:
                try:
                    mid = await asyncio.wait_for(
                        price_feed.get_price(row["market_id"]), timeout=3.0
                    )
                    if mid is not None:
                        mark_price = Decimal(str(mid))
                        entry_price = Decimal(row["price"])
                        quantity = size / entry_price if entry_price > 0 else Decimal("0")
                        mark_pnl += (mark_price - entry_price) * quantity
                except Exception:
                    pass

        snapshot = {
            "open_positions": len(open_orders),
            "total_exposure_usdc": str(total_exposure),
            "mark_to_market_pnl": str(mark_pnl),
            "realized_pnl_all_time": str(stats.get("total_pnl", Decimal("0"))),
            "hit_rate": stats.get("hit_rate"),
            "markets": list({row["market_id"] for row in open_orders}),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        logger.info("shutdown_snapshot", **{k: str(v) for k, v in snapshot.items()})
        return snapshot
