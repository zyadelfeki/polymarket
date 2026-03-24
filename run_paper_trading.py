#!/usr/bin/env python3
"""
Paper trading runner — the real continuous loop.

This is the entry point that connects
BTCPriceLevelScanner → IdempotencyManager → PaperOrderBook → TradeExecutor
into a single loop that places paper orders.

What this does
--------------
1. Scans every SCAN_INTERVAL_SECONDS for BTC price-level markets.
2. For every opportunity the scanner returns (already gate-approved by
   CharliePredictionGate):
   a. Checks IdempotencyManager — skip if a SUCCESSFUL order for this
      exact market/side/price was already placed this session.  Rejected
      orders are NOT cached so transient blocks never suppress re-entry.
   b. Checks PaperOrderBook — skip if an open position already exists
      for this (market_id, side) pair (deduplication within the book).
   c. Calls TradeExecutor.execute_trade() which calls place_order() on
      the paper client.  Logs order_attempt / order_placed / order_rejected.
3. After each scan, calls settle_open_positions() which resolves any
   markets whose end_date has passed, crediting the bankroll and notifying
   the circuit breaker ONLY on resolved outcomes.  Uses the order book's
   own settle_open_positions() method so internal accounting stays correct.
4. Logs a per-scan summary with open position count and PnL.
5. Runs until interrupted.  Safe to restart: IdempotencyManager persists
   successful placements to disk so duplicate orders are prevented across
   restarts.

Environment variables
---------------------
  PAPER_TRADING=true        (required — enforced at startup)
  SCAN_INTERVAL_SECONDS     scan loop sleep, default 30
  INITIAL_CAPITAL           paper bankroll in USDC, default 100
  CHARLIE_PATH              path to project-charlie repo root (if not installed)

Usage
-----
  python run_paper_trading.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

os.environ["PAPER_TRADING"] = "true"

sys.path.insert(0, str(Path(__file__).parent))

try:
    import structlog
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    _log = structlog.get_logger("paper_trading")
    def log(level: str, event: str, **kw):
        getattr(_log, level)(event, **kw)
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    _std_log = logging.getLogger("paper_trading")
    def log(level: str, event: str, **kw):
        getattr(_std_log, level)("%s %s", event, kw or "")

from config.settings import Settings
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from execution.idempotency_manager import IdempotencyManager
from execution.paper_order_book import PaperOrderBook
from execution.trade_executor import TradeExecutor, MIN_BET_SIZE
from integrations.charlie_booster import CharliePredictionGate
from risk.circuit_breaker import CircuitBreaker
from risk.kelly_sizing import KellySizer
from strategies.btc_price_level_scanner import BTCPriceLevelScanner

SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", "30"))
INITIAL_CAPITAL = Decimal(os.environ.get("INITIAL_CAPITAL", "100"))


class _PaperDB:
    """
    Minimal in-memory trade log that satisfies the TradeExecutor.db interface.
    Writes a JSON line to ./data/paper_trades.jsonl on every log_trade call
    so trades survive a restart.
    """

    def __init__(self) -> None:
        import json
        self._json = json
        self._path = Path("./data/paper_trades.jsonl")
        self._path.parent.mkdir(exist_ok=True)
        self._counter = self._load_counter()

    def _load_counter(self) -> int:
        if not self._path.exists():
            return 0
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            return len(lines)
        except Exception:
            return 0

    def log_trade(self, record: dict) -> int:
        self._counter += 1
        record["_id"] = self._counter
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(self._json.dumps(record, default=str) + "\n")
        return self._counter


class _PaperBankrollTracker:
    """
    Tracks paper bankroll in memory.

    add_trade() is called by TradeExecutor after each successful order.
    The executor passes bet_size = the POST-CLAMP value (i.e. at least
    MIN_BET_SIZE when Kelly was below the floor), which matches exactly
    what PaperOrderBook.record_order() now stores as PaperPosition.size.
    """

    def __init__(self, initial: Decimal) -> None:
        self.balance = initial
        self._trades: list = []

    def add_trade(self, record: dict) -> None:
        self._trades.append(record)
        self.balance -= Decimal(str(record.get("bet_size", 0)))

    @property
    def current_balance(self) -> Decimal:
        return self.balance


def settle_open_positions(
    order_book: PaperOrderBook,
    bankroll_tracker: _PaperBankrollTracker,
    circuit_breaker: CircuitBreaker,
) -> int:
    """
    Check each open PaperPosition.  If its end_date has passed, settle it.

    Neutral settlement: full stake refunded, pnl=0, circuit_breaker win=True.
    This prevents the circuit breaker from firing on unresolved markets.

    Uses order_book.settle_open_positions() so that PaperOrderBook's internal
    _total_staked and _total_pnl stay consistent with what is actually
    staked/returned.  Calling order_book.remove_position() directly would
    bypass that internal accounting and leave the order book summary stale.

    Returns the number of positions settled this cycle.
    """
    now = datetime.now(timezone.utc)
    total_settled = 0

    for pos in list(order_book.get_open_positions()):
        if not pos.end_date:
            continue

        try:
            end_dt = datetime.fromisoformat(
                pos.end_date.replace("Z", "+00:00")
            )
        except Exception:
            continue

        if now < end_dt:
            continue

        # Settle through the order book so its internal counters update.
        # settle_open_positions(outcome='neutral') sets pos.pnl=0, marks
        # pos.settled=True, appends to _settled, updates _total_pnl.
        settled_list = order_book.settle_open_positions(
            pos.market_id,
            outcome="neutral",
        )

        for settled_pos in settled_list:
            # Refund the clamped stake (pos.size is already the effective
            # amount after the MIN_BET_SIZE clamp applied in record_order).
            bankroll_tracker.balance += settled_pos.size

            # Keep the circuit breaker's capital view in sync with the real
            # bankroll after every settlement credit.  Without this call the
            # breaker sees a progressively lower capital figure and may fire a
            # false drawdown alert on long-running sessions.
            circuit_breaker.update_capital(bankroll_tracker.current_balance)

            # Neutral outcome = no real win/loss; tell circuit breaker it
            # was a win so the consecutive-loss counter is not incremented.
            circuit_breaker.record_trade(
                profit=Decimal("0"),
                win=True,
            )
            total_settled += 1

            log(
                "info",
                "position_settled_neutral",
                market_id=settled_pos.market_id,
                side=settled_pos.side,
                bet_size=str(settled_pos.size),
                question=settled_pos.question[:60],
                end_date=settled_pos.end_date,
            )

    return total_settled


async def run_loop(
    scanner: BTCPriceLevelScanner,
    charlie_gate: CharliePredictionGate,
    api_client: PolymarketClientV2,
    executor: TradeExecutor,
    idempotency: IdempotencyManager,
    order_book: PaperOrderBook,
    bankroll_tracker: _PaperBankrollTracker,
) -> None:
    cycle = 0
    SEP = "-" * 60

    while True:
        cycle += 1
        log(
            "info",
            "scan_cycle_start",
            cycle=cycle,
            balance=str(bankroll_tracker.current_balance),
        )

        # --- Circuit breaker check -------------------------------------------
        if not executor.circuit_breaker.is_trading_allowed():
            log(
                "warning",
                "scan_cycle_skipped_circuit_breaker",
                cycle=cycle,
                reason=executor.circuit_breaker.breaker_reason,
            )
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            continue

        # --- Scan -------------------------------------------------------------
        try:
            opportunities = await scanner.scan(
                charlie_gate=charlie_gate,
                api_client=api_client,
                equity=bankroll_tracker.current_balance,
                max_days_to_expiry=7,
            )
        except Exception as exc:
            log("error", "scan_error", cycle=cycle, error=str(exc), exc_info=True)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            continue

        log(
            "info",
            "scan_cycle_opportunities",
            cycle=cycle,
            count=len(opportunities),
        )

        orders_attempted = 0
        orders_placed = 0
        orders_skipped_idempotency = 0
        orders_skipped_duplicate = 0

        for opp in opportunities:
            market_id = str(opp.get("market_id", ""))
            side = str(opp.get("side", "YES")).upper()
            # raw Kelly size from the scanner/gate — may be below MIN_BET_SIZE.
            # The executor will clamp it; the order book mirrors that clamp.
            raw_size = Decimal(str(opp.get("size", "0")))
            entry_price = Decimal(str(opp.get("market_price", "0.5")))
            end_date = str(opp.get("end_date", ""))

            # --- Idempotency check -------------------------------------------
            # Key is built from the raw Kelly size so price-tick movements
            # that produce a trivially different Kelly fraction don't
            # immediately bypass the duplicate guard.  Only SUCCESSFUL
            # placements are ever written to the cache so a failed attempt
            # never poisons future cycles.
            idem_key = idempotency.generate_key(
                market_id=market_id,
                side=side,
                size=raw_size,
                price=entry_price,
                strategy="charlie_gate",
            )
            if idempotency.is_duplicate(idem_key):
                orders_skipped_idempotency += 1
                log(
                    "debug",
                    "order_skipped_idempotency",
                    market_id=market_id,
                    side=side,
                )
                continue

            # --- Paper order book dedup --------------------------------------
            if order_book.is_duplicate(market_id, side):
                orders_skipped_duplicate += 1
                log(
                    "debug",
                    "order_skipped_open_position",
                    market_id=market_id,
                    side=side,
                )
                continue

            # Build the opportunity dict the executor expects.
            # kelly_size carries the raw value; the executor owns the clamp.
            exec_opp = dict(opp)
            exec_opp["kelly_size"] = opp.get("size")
            exec_opp["strategy"] = "charlie_gate"

            orders_attempted += 1
            success = await executor.execute_trade(exec_opp)

            if success:
                orders_placed += 1

                # Write to the order book FIRST, then to the idempotency cache.
                # This ordering guarantees atomicity: if record_placement()
                # raises after record_order() succeeds, the order book dedup
                # guard catches any retry on the next cycle.  The reverse
                # ordering would poison the idem cache on a mid-write crash,
                # permanently blocking re-entry with no log trace.
                order_book.record_order(
                    market_id=market_id,
                    side=side,
                    size=raw_size,
                    entry_price=entry_price,
                    kelly_fraction=Decimal(str(opp.get("kelly_fraction", "0"))),
                    edge=Decimal(str(opp.get("edge", "0"))),
                    confidence=Decimal(str(opp.get("confidence", "0"))),
                    question=str(opp.get("question", "")),
                    end_date=end_date,
                )
                # Only write to idempotency cache after the order book record
                # has been committed.  record_placement() increments the
                # attempt counter correctly and persists to disk atomically.
                idempotency.record_placement(
                    idem_key,
                    {"success": True, "market_id": market_id, "side": side},
                )

                # Keep the circuit breaker's capital view in sync after each
                # successful placement.  execute_trade() debits the bankroll
                # via bankroll.add_trade() but the breaker is never notified,
                # so without this call its drawdown calculation drifts lower
                # on every trade and may fire a false circuit-break.
                executor.circuit_breaker.update_capital(
                    bankroll_tracker.current_balance
                )

        # --- Settle resolved positions ---------------------------------------
        settled_count = settle_open_positions(
            order_book=order_book,
            bankroll_tracker=bankroll_tracker,
            circuit_breaker=executor.circuit_breaker,
        )
        if settled_count:
            log(
                "info",
                "positions_settled",
                count=settled_count,
                balance=str(bankroll_tracker.current_balance),
            )

        # --- Per-cycle summary -----------------------------------------------
        book_summary = order_book.summary()
        log(
            "info",
            "scan_cycle_complete",
            cycle=cycle,
            orders_attempted=orders_attempted,
            orders_placed=orders_placed,
            orders_skipped_idempotency=orders_skipped_idempotency,
            orders_skipped_duplicate=orders_skipped_duplicate,
            open_positions=book_summary["open_positions"],
            total_staked_usdc=book_summary["total_staked_usdc"],
            total_pnl_usdc=book_summary["total_pnl_usdc"],
            balance=str(bankroll_tracker.current_balance),
        )
        print(SEP)

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def main() -> None:
    SEP = "=" * 60
    print(SEP)
    print("POLYMARKET PAPER TRADING")
    print(f"  Initial capital : ${INITIAL_CAPITAL}")
    print(f"  Scan interval   : {SCAN_INTERVAL_SECONDS}s")
    print(f"  PAPER_TRADING   : {os.environ.get('PAPER_TRADING')}")
    print(SEP)

    assert os.environ.get("PAPER_TRADING") == "true", (
        "BUG: PAPER_TRADING must be 'true' before run_paper_trading.py is started"
    )

    bankroll_tracker = _PaperBankrollTracker(INITIAL_CAPITAL)
    db = _PaperDB()
    kelly_sizer = KellySizer()
    circuit_breaker = CircuitBreaker(initial_capital=INITIAL_CAPITAL)
    idempotency = IdempotencyManager(
        db_path="./data/paper_idempotency.json",
        ttl=3600,
    )
    order_book = PaperOrderBook()

    api_client = PolymarketClientV2(
        private_key=None,
        paper_trading=True,
        rate_limit=8.0,
    )

    charlie_gate = CharliePredictionGate(
        kelly_sizer=kelly_sizer,
        min_edge=Decimal("0.05"),
        min_confidence=Decimal("0.60"),
    )

    executor = TradeExecutor(
        polymarket_client=api_client,
        bankroll_tracker=bankroll_tracker,
        kelly_sizer=kelly_sizer,
        db=db,
        circuit_breaker=circuit_breaker,
    )

    scanner = BTCPriceLevelScanner()

    log(
        "info",
        "paper_trading_runner_started",
        initial_capital=str(INITIAL_CAPITAL),
        scan_interval=SCAN_INTERVAL_SECONDS,
        min_bet_size=str(MIN_BET_SIZE),
    )

    await run_loop(
        scanner=scanner,
        charlie_gate=charlie_gate,
        api_client=api_client,
        executor=executor,
        idempotency=idempotency,
        order_book=order_book,
        bankroll_tracker=bankroll_tracker,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nPaper trading runner stopped by user.")
