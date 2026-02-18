import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import config_production as config
from config_production import (
    API_CONFIG,
    CIRCUIT_BREAKER_CONFIG,
    KELLY_CONFIG,
    LOGGING_CONFIG,
    SAFETY_CONFIG,
    STARTING_CAPITAL,
    STRATEGY_CONFIG,
)
from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger
from risk.kelly_sizer import AdaptiveKellySizer
from risk.circuit_breaker_v2 import CircuitBreakerV2
from services.execution_service import ExecutionService
from strategies.latency_arbitrage_btc import LatencyArbitrageEngine
from utils.decimal_helpers import quantize_price, quantize_quantity

KILL_SWITCH_PATH = Path("KILL_SWITCH_ACTIVE.flag")
DEFAULT_DB_PATH = "data/trading.db"
DEFAULT_HEARTBEAT_PATH = Path("runtime/heartbeat.txt")
PAPER_MODE_STATIC_BALANCE = Decimal("20")
PAPER_MODE_NETWORK_PARTITION_THRESHOLD_SECONDS = 60


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOGGING_CONFIG["log_level"], logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOGGING_CONFIG["log_file"], encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    logging.getLogger("phase6").setLevel(getattr(logging, LOGGING_CONFIG["log_level"], logging.INFO))


class ProductionBot:
    def __init__(self, mode: str, db_path: str, interval_seconds: int) -> None:
        self.mode = mode
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.logger = logging.getLogger("phase6")
        self.running = False

        self.ledger: Optional[AsyncLedger] = None
        self.binance: Optional[BinanceWebSocketV2] = None
        self.client: Optional[PolymarketClientV2] = None
        self.execution_service: Optional[ExecutionService] = None
        self.kelly_sizer: Optional[AdaptiveKellySizer] = None
        self.strategy: Optional[LatencyArbitrageEngine] = None
        self.circuit_breaker: Optional[CircuitBreakerV2] = None
        self._seen_order_keys: set[str] = set()
        self.heartbeat_path = Path(getattr(config, "HEARTBEAT_FILE", DEFAULT_HEARTBEAT_PATH))
        self.last_known_balance: Decimal = Decimal("0")

    async def initialize(self) -> None:
        self.logger.info("🔵 STARTING initialize() method")
        self.logger.info("INIT STEP 1/7: creating AsyncLedger")

        self.ledger = AsyncLedger(db_path=self.db_path)
        await self.ledger.initialize()
        self.logger.info("✅ INIT STEP 1/7 complete: AsyncLedger initialized")

        # Clear positions left open by previous crashed/stopped runs.  These are
        # phantom rows — the bot never actually closed them — and they inflate the
        # aggregate exposure counter, causing every new trade to be rejected.
        stale_count = await self.ledger.close_stale_positions()
        open_positions_after = await self.ledger.get_open_positions()
        open_exposure_after = sum(
            (p.entry_price * p.quantity for p in open_positions_after), Decimal("0")
        )
        self.logger.info(
            "startup_position_cleanup | stale_closed=%s | remaining_open=%s | remaining_exposure=%s",
            stale_count,
            len(open_positions_after),
            open_exposure_after,
        )

        self.logger.info("INIT STEP 2/7: loading equity from ledger")
        equity = await self.ledger.get_equity()
        if equity <= 0:
            await self.ledger.record_deposit(STARTING_CAPITAL, "Phase 6 starting capital")
            equity = await self.ledger.get_equity()

        if self.mode == "paper":
            previous_equity = equity
            if equity < PAPER_MODE_STATIC_BALANCE:
                top_up = PAPER_MODE_STATIC_BALANCE - equity
                await self.ledger.record_deposit(top_up, "Paper mode balance top-up")
                equity = await self.ledger.get_equity()
            elif equity > PAPER_MODE_STATIC_BALANCE:
                self.logger.warning(
                    "paper_balance_above_target | current=%s target=%s | no_auto_withdraw",
                    equity,
                    PAPER_MODE_STATIC_BALANCE,
                )

            self.last_known_balance = PAPER_MODE_STATIC_BALANCE
            self.logger.info(
                "paper_balance_fixed | previous=%s current=%s change=%s ledger_equity=%s",
                previous_equity,
                self.last_known_balance,
                self.last_known_balance - previous_equity,
                equity,
            )
        else:
            self.last_known_balance = equity

        self.logger.info("✅ INIT STEP 2/7 complete: equity loaded | equity=%s", equity)

        self.logger.info("INIT STEP 3/7: creating CircuitBreakerV2")
        self.circuit_breaker = CircuitBreakerV2(
            initial_equity=equity,
            max_drawdown_pct=float(CIRCUIT_BREAKER_CONFIG["max_drawdown_pct"]),
            max_loss_streak=int(CIRCUIT_BREAKER_CONFIG["max_consecutive_losses"]),
            daily_loss_limit_pct=float((CIRCUIT_BREAKER_CONFIG["max_daily_loss"] / STARTING_CAPITAL) * Decimal("100")),
            adaptive_risk_profile=bool(CIRCUIT_BREAKER_CONFIG.get("adaptive_risk_profile", True)),
        )
        self.logger.info("✅ INIT STEP 3/7 complete: CircuitBreakerV2 created")

        self.logger.info("INIT STEP 4/7: creating BinanceWebSocketV2")
        self.binance = BinanceWebSocketV2(symbols=["BTC"])
        self.logger.info("INIT STEP 4/7: starting BinanceWebSocketV2")
        started = await self.binance.start()
        if not started:
            raise RuntimeError("Failed to start Binance WebSocket")
        self.logger.info("✅ INIT STEP 4/7 complete: BinanceWebSocketV2 started")

        self.logger.info("INIT STEP 5/7: creating PolymarketClientV2")
        try:
            self.client = PolymarketClientV2(
                paper_trading=(self.mode == "paper"),
                max_retries=API_CONFIG["max_retries"],
                timeout=API_CONFIG["request_timeout_seconds"],
            )
            self.logger.info("✅ INIT STEP 5/7 complete: PolymarketClientV2 created")
        except Exception as exc:
            self.logger.error("❌ PolymarketClientV2 creation failed: %s", exc, exc_info=True)
            raise

        self.logger.info("⏳ Waiting 5 seconds for client to fully initialize...")
        await asyncio.sleep(5)
        self.logger.info("⏳ Client should be ready now, creating ExecutionService...")

        self.logger.info("INIT STEP 6/7: creating ExecutionService")
        try:
            self.execution_service = ExecutionService(
                polymarket_client=self.client,
                ledger=self.ledger,
                config={
                    "max_retries": API_CONFIG["max_retries"],
                    "timeout_seconds": API_CONFIG["request_timeout_seconds"],
                    # Temporary testing override for paper mode only.
                    # Production/live mode keeps the stricter default threshold.
                    "partition_threshold_seconds": (
                        PAPER_MODE_NETWORK_PARTITION_THRESHOLD_SECONDS if self.mode == "paper" else 15
                    ),
                },
            )
            self.logger.info("✅ ExecutionService created successfully")
            self.logger.info("✅ INIT STEP 6/7 complete: ExecutionService created")
        except Exception as exc:
            self.logger.error("❌ ExecutionService creation failed: %s", exc, exc_info=True)
            raise

        self.logger.info("INIT STEP 7/7: creating AdaptiveKellySizer")
        try:
            self.kelly_sizer = AdaptiveKellySizer(
                config={
                    "kelly_fraction": str(KELLY_CONFIG["fractional_kelly"]),
                    "conservative_kelly": str(KELLY_CONFIG.get("conservative_kelly", KELLY_CONFIG["fractional_kelly"])),
                    "aggressive_kelly": str(KELLY_CONFIG.get("aggressive_kelly", Decimal("1.0"))),
                    "growth_mode_threshold": str(KELLY_CONFIG.get("growth_mode_threshold", Decimal("200.0"))),
                    "growth_max_bet_pct": str(KELLY_CONFIG.get("growth_max_bet_pct", Decimal("20.0"))),
                    "round_up_min_edge": str(KELLY_CONFIG.get("round_up_min_edge", Decimal("0.05"))),
                    "growth_max_kelly_fraction": "1.0",
                    "max_bet_pct": str(KELLY_CONFIG["max_bet_pct"]),
                    "min_edge": str(KELLY_CONFIG["min_edge_required"]),
                    "max_aggregate_exposure": "50.0",  # 50% of bankroll; was 20% (too tight for $20 paper account)
                    "min_bet_size": "1.0",
                }
            )
            self.logger.info("✅ AdaptiveKellySizer created successfully")
            self.logger.info("✅ INIT STEP 7/7 complete: AdaptiveKellySizer created")
        except Exception as exc:
            self.logger.error("❌ AdaptiveKellySizer creation failed: %s", exc, exc_info=True)
            raise

        self.logger.info("INIT POST-STEP: creating LatencyArbitrageEngine")
        try:
            self.strategy = LatencyArbitrageEngine(
                binance_ws=self.binance,
                polymarket_client=self.client,
                charlie_predictor=None,
                execution_service=self.execution_service,
                kelly_sizer=self.kelly_sizer,
                config={
                    "min_edge": str(STRATEGY_CONFIG["min_edge"]),
                    "min_time_left_seconds": STRATEGY_CONFIG["min_time_to_expiry_seconds"],
                    "max_time_left_seconds": STRATEGY_CONFIG["time_window_minutes"] * 60,
                },
            )
            self.logger.info("✅ LatencyArbitrageEngine created successfully")
            self.logger.info("✅ INIT POST-STEP complete: LatencyArbitrageEngine created")
        except Exception as exc:
            self.logger.error("❌ LatencyArbitrageEngine creation failed: %s", exc, exc_info=True)
            raise

        self.logger.info(f"DEBUG: About to check mode. self.mode={self.mode}, type={type(self.mode)}")
        if self.mode == "paper":
            self.logger.info(
                "paper_mode_balance_confirmed | current=%s",
                self.last_known_balance,
            )
        else:
            live_balance = await self.client.get_live_balance() if self.client else None
            if live_balance is not None and live_balance > Decimal("0"):
                previous_balance = self.last_known_balance
                self.last_known_balance = live_balance
                self.logger.info(
                    "balance_synced | previous=%s current=%s change=%s",
                    previous_balance,
                    live_balance,
                    live_balance - previous_balance,
                )
                self.logger.info("Live balance initialized | balance=%s", live_balance)
            else:
                self.logger.warning("Live balance unavailable at init; using ledger equity=%s", equity)

        self.logger.info("Initialization complete | mode=%s | equity=%s", self.mode, equity)
        self.logger.info("🟢 FINISHED initialize() method")

    async def safety_ok(self) -> bool:
        if SAFETY_CONFIG["enable_kill_switch_check"] and KILL_SWITCH_PATH.exists():
            self.logger.error("Kill switch flag detected; halting trading loop")
            return False

        if not self.ledger or not self.circuit_breaker:
            return False

        equity = await self.ledger.get_equity()
        can_trade = await self.circuit_breaker.can_trade(current_equity=equity, position_size_pct=float(KELLY_CONFIG["max_bet_pct"]))
        if not can_trade:
            self.logger.warning("Circuit breaker blocks trading | status=%s", self.circuit_breaker.get_status())
            return False

        if equity <= Decimal("1.00"):
            self.logger.error("Critical balance floor reached: %s", equity)
            return False

        return True

    async def execute_once(self) -> None:
        if not self.strategy or not self.ledger or not self.execution_service or not self.kelly_sizer:
            raise RuntimeError("Bot not initialized")

        if not await self.safety_ok():
            return

        opportunity = await self.strategy.scan_opportunities()
        if not opportunity:
            self.logger.info("No opportunities found")
            return

        # Fetch open positions first — needed to compute available cash in paper mode.
        open_positions = await self.ledger.get_open_positions()
        current_exposure = sum((p.entry_price * p.quantity for p in open_positions), Decimal("0"))

        if self.mode == "paper":
            # Paper mode: size against *available* cash, not the static total balance.
            # Available cash = static balance − already-deployed capital.
            # Passing available_cash as bankroll with current_exposure=0 avoids
            # double-counting (exposure is already baked into the reduced bankroll).
            available_cash = max(PAPER_MODE_STATIC_BALANCE - current_exposure, Decimal("0"))
            capital = available_cash
            self.last_known_balance = PAPER_MODE_STATIC_BALANCE  # report total, not available
            self.logger.info(
                "paper_available_cash | total=%s deployed=%s available=%s",
                PAPER_MODE_STATIC_BALANCE,
                current_exposure,
                available_cash,
            )
        else:
            live_balance = await self.client.get_live_balance() if self.client else None
            if live_balance is None:
                self.logger.warning("Live balance fetch failed; using cached balance=%s", self.last_known_balance)
                capital = self.last_known_balance
            else:
                previous_balance = self.last_known_balance
                capital = live_balance
                self.last_known_balance = live_balance
                self.logger.info(
                    "balance_synced | previous=%s current=%s change=%s",
                    previous_balance,
                    live_balance,
                    live_balance - previous_balance,
                )

        # Fallback for live mode only: paper mode can legitimately have capital=0
        # (all cash deployed) and we must not override that with the total equity.
        if capital <= 0 and self.mode != "paper":
            capital = await self.ledger.get_equity()
            self.last_known_balance = capital

        self.logger.info("Scan cycle sizing balance=%s", capital)

        market_price = quantize_price(Decimal(str(opportunity.get("market_price", "0"))))
        edge = Decimal(str(opportunity.get("edge", "0")))
        if market_price <= 0:
            self.logger.warning("Invalid market price in opportunity: %s", opportunity)
            return

        # In paper mode the bankroll is already available cash (total minus deployed),
        # so passing current_exposure again would double-count.  Live mode uses the
        # raw live balance from the exchange, so the ledger exposure is still needed.
        sizing_exposure = Decimal("0") if self.mode == "paper" else current_exposure
        size_result = self.kelly_sizer.calculate_bet_size(
            bankroll=capital,
            edge=edge,
            market_price=market_price,
            current_aggregate_exposure=sizing_exposure,
        )
        if size_result.size <= 0:
            self.logger.info(
                "Sizing rejected opportunity | reason=%s | risk_warning=%s",
                size_result.capped_reason,
                size_result.risk_warning,
            )
            return

        if size_result.adjusted:
            self.logger.warning(
                "Position adjusted | mode=%s | adjusted_size=%s | reason=%s",
                size_result.mode,
                size_result.size,
                size_result.risk_warning,
            )

        quantity = quantize_quantity(size_result.size / market_price)
        if quantity <= 0:
            self.logger.info("Quantity rounded to zero; skipping")
            return

        if self.mode == "paper" and self.execution_service is not None:
            # Temporary paper-mode testing workaround:
            # keep execution network health fresh from successful scan cycles
            # until proper websocket/API reconnection handling is implemented.
            self.execution_service.network_monitor.record_success()

        order_key = f"{opportunity.get('market_id')}:{opportunity.get('token_id')}:{opportunity.get('side')}:{market_price}:{quantity}"
        if order_key in self._seen_order_keys:
            self.logger.warning("Duplicate order prevented by in-bot idempotency guard")
            return

        result = await self.execution_service.place_order(
            strategy="latency_arbitrage_btc",
            market_id=str(opportunity.get("market_id")),
            token_id=str(opportunity.get("token_id")),
            side=str(opportunity.get("side")),
            quantity=quantity,
            price=market_price,
            metadata={"edge": str(edge), "outcome": str(opportunity.get("outcome"))},
        )
        self._seen_order_keys.add(order_key)

        if result and getattr(result, "success", False):
            self.logger.info(
                "Trade executed | market=%s side=%s edge=%s qty=%s",
                opportunity.get("market_id"),
                opportunity.get("side"),
                opportunity.get("edge"),
                quantity,
            )
        else:
            self.logger.warning("Trade failed | result=%s", result)

    async def run(self, once: bool) -> None:
        self.running = True
        if once:
            self._write_heartbeat()
            await self.execute_once()
            return

        while self.running:
            try:
                self._write_heartbeat()
                await self.execute_once()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.exception("Trading loop error: %s", exc)
                await asyncio.sleep(30)

    def _write_heartbeat(self) -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")

    async def shutdown(self) -> None:
        self.running = False
        if self.binance:
            await self.binance.stop()
        if self.client:
            await self.client.close()
        if self.ledger:
            final_equity = await self.ledger.get_equity()
            pnl = final_equity - STARTING_CAPITAL
            self.logger.info("Shutdown complete | final_equity=%s | pnl=%s", final_equity, pnl)
            await self.ledger.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 6 production bot runner")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--interval", type=int, default=STRATEGY_CONFIG["scan_interval_seconds"])
    parser.add_argument("--once", action="store_true", help="Run one scan/execute cycle and exit")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    configure_logging()

    banner = (
        "\n"
        "============================================================\n"
        "POLYMARKET LATENCY ARB BOT - PHASE 6\n"
        f"Mode: {args.mode.upper()} | Starting capital: ${STARTING_CAPITAL}\n"
        "Safety: circuit breaker + kill switch + idempotency\n"
        "============================================================"
    )
    print(banner)

    bot = ProductionBot(mode=args.mode, db_path=args.db_path, interval_seconds=args.interval)
    try:
        await bot.initialize()
        await bot.run(once=args.once)
        return 0
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
