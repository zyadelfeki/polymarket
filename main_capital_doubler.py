#!/usr/bin/env python3
"""
THE MAIN BOT - Run this 24/7
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal, getcontext
from typing import Optional, Dict

from config.settings import settings
from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from data_feeds.price_history import PriceHistory
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from data_feeds.zmq_price_subscriber import ZMQPriceSubscriber
from data_feeds.redis_intelligence_subscriber import RedisIntelligenceSubscriber
from database.ledger_async import AsyncLedger
from execution.ultra_fast_executor import UltraFastExecutor
from integrations.charlie_booster import CharliePredictionBooster
from risk.capital_protection import CircuitBreaker
from services.execution_service_v2 import ExecutionServiceV2
from strategies.latency_arbitrage_btc import LatencyArbitrageEngine
from strategies.mispricing_hunter import MispricingHunter
from utils.decimal_helpers import to_decimal, to_timeout_float

logger = logging.getLogger(__name__)

UVLOOP_AVAILABLE = False
if sys.platform != "win32":
    try:
        import uvloop

        uvloop.install()
        UVLOOP_AVAILABLE = True
        logger.info("uvloop_installed")
    except Exception as exc:
        logger.warning("uvloop_unavailable | error=%s", str(exc))
else:
    logger.info("uvloop_unavailable | platform=win32")

getcontext().prec = 18

CHARLIE_MODEL_PATH = os.environ.get(
    "CHARLIE_MODEL_PATH",
    "C:/Users/zyade/Charlie2/models/lstm_model.h5",
)
CHARLIE_CONFIG_PATH = os.environ.get(
    "CHARLIE_CONFIG_PATH",
    "C:/Users/zyade/Charlie2/config/config.json",
)


def validate_charlie_paths() -> None:
    """Verify Charlie files exist before starting."""
    if not os.path.exists(CHARLIE_MODEL_PATH):
        logger.error("charlie_model_not_found | path=%s", CHARLIE_MODEL_PATH)
        raise FileNotFoundError(f"Charlie model not found: {CHARLIE_MODEL_PATH}")

    if not os.path.exists(CHARLIE_CONFIG_PATH):
        logger.error("charlie_config_not_found | path=%s", CHARLIE_CONFIG_PATH)
        raise FileNotFoundError(f"Charlie config not found: {CHARLIE_CONFIG_PATH}")

    logger.info(
        "charlie_paths_validated | model=%s config=%s",
        CHARLIE_MODEL_PATH,
        CHARLIE_CONFIG_PATH,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capital Doubler Bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--capital",
        type=str,
        default="13.98",
        help="Starting capital (default: 13.98)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Run for N seconds then stop (default: run forever)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--no-charlie",
        action="store_true",
        help="Run without Charlie LSTM (latency only)",
    )
    return parser.parse_args()


class CapitalDoublerBot:
    def __init__(
        self,
        starting_capital: Decimal,
        mode: str = "paper",
        config: Optional[Dict] = None,
    ) -> None:
        self.capital = Decimal(str(starting_capital))
        self.mode = mode
        self.config = config or {}
        self.scan_interval = to_decimal(self.config.get("scan_interval", "0.5"))
        self.no_charlie = bool(self.config.get("no_charlie", False))

        self.is_running = False

        self.ledger: Optional[AsyncLedger] = None
        self.polymarket: Optional[PolymarketClientV2] = None
        self.binance: Optional[BinanceWebSocketV2] = None
        self.charlie: Optional[CharliePredictionBooster] = None
        self.execution_service: Optional[ExecutionServiceV2] = None
        self.strategy: Optional[LatencyArbitrageEngine] = None
        self.executor: Optional[UltraFastExecutor] = None
        self.risk: Optional[CircuitBreaker] = None
        self.price_history: Optional[PriceHistory] = None
        self.mispricing_hunter: Optional[MispricingHunter] = None
        self.price_subscriber: Optional[ZMQPriceSubscriber] = None
        self.intelligence_subscriber: Optional[RedisIntelligenceSubscriber] = None

    def set_ipc_subscribers(
        self,
        price_subscriber: ZMQPriceSubscriber,
        intelligence_subscriber: RedisIntelligenceSubscriber,
    ) -> None:
        self.price_subscriber = price_subscriber
        self.intelligence_subscriber = intelligence_subscriber

    async def _on_price_update(self, symbol: str, price_data) -> None:
        if not self.price_history:
            return
        try:
            self.price_history.record_price(symbol, price_data.price)
        except Exception as exc:
            logger.debug("price_history_record_failed | error=%s", str(exc))

    async def initialize(self) -> None:
        if self.strategy and self.executor:
            return

        ledger = AsyncLedger(db_path=settings.DATABASE_PATH)
        await ledger.initialize()

        polymarket = PolymarketClientV2(
            private_key=settings.POLYMARKET_PRIVATE_KEY,
            api_key=settings.POLYMARKET_API_KEY,
            paper_trading=(self.mode == "paper"),
        )

        self.price_history = PriceHistory()
        binance = BinanceWebSocketV2(symbols=["BTC"], on_price_update=self._on_price_update)
        await binance.start()

        charlie = None
        if not self.no_charlie:
            try:
                validate_charlie_paths()
                charlie = CharliePredictionBooster()
                logger.info("charlie_loaded")
            except FileNotFoundError as exc:
                logger.warning("charlie_not_found | error=%s mode=latency_only", str(exc))
                charlie = None
        else:
            logger.info("charlie_disabled | reason=no_charlie_flag")

        execution_service = ExecutionServiceV2(
            polymarket_client=polymarket,
            ledger=ledger,
            config={
                "max_retries": 2,
                "timeout_seconds": "0.5",
                "fill_check_interval": "0.2",
            },
        )
        await execution_service.start()

        strategy = LatencyArbitrageEngine(
            binance_ws=binance,
            polymarket_client=polymarket,
            charlie_predictor=charlie,
            config={
                "min_edge": to_decimal(self.config.get("min_edge", "0.03")),
                "max_edge": to_decimal(self.config.get("max_edge", "0.50")),
                "min_volatility_pct": to_decimal(self.config.get("min_volatility_pct", "0.0")),
            },
            price_history=self.price_history,
            redis_subscriber=self.intelligence_subscriber,
        )

        self.mispricing_hunter = MispricingHunter(
            binance_feed=binance,
            min_edge=Decimal("0.05"),
        )

        executor = UltraFastExecutor(
            execution_service=execution_service,
            ledger=ledger,
            charlie_booster=charlie,
            config={
                "order_timeout_seconds": "0.5",
                "limit_price_buffer": "0.01",
            },
        )

        starting_equity = await ledger.get_equity()
        if starting_equity <= 0:
            logger.warning("ledger_equity_zero | fallback=%s", self.capital)
            starting_equity = self.capital

        risk = CircuitBreaker(starting_capital=starting_equity)

        self.ledger = ledger
        self.polymarket = polymarket
        self.binance = binance
        self.charlie = charlie
        self.execution_service = execution_service
        self.strategy = strategy
        self.executor = executor
        self.risk = risk

    async def scan_once(self) -> Optional[Dict]:
        try:
            await self.initialize()
        except Exception as exc:
            logger.error("initialization_failed | error=%s", str(exc))
            return None

        if not self.strategy:
            return None

        if self.mispricing_hunter and self.polymarket:
            try:
                if hasattr(self.polymarket, "get_crypto_15min_markets"):
                    markets = await self.polymarket.get_crypto_15min_markets()
                else:
                    markets = await self.polymarket.get_markets(active=True, limit=200)
                opportunities = await self.mispricing_hunter.scan_for_mispricings(markets)
                if opportunities:
                    opp = opportunities[0]
                    side = "YES" if opp.get("signal") == "BUY_YES" else "NO"
                    return {
                        "market_id": opp["market_id"],
                        "token_id": opp["token_id"],
                        "side": side,
                        "true_prob": opp["confidence"],
                        "market_price": opp["entry_price"],
                        "edge": opp["edge_net"],
                        "charlie_confidence": Decimal("0.5"),
                        "question": opp.get("question"),
                    }
            except Exception as exc:
                logger.warning("mispricing_scan_failed | error=%s", str(exc))

        return await self.strategy.scan_opportunities()

    def record_loss(self, amount: Decimal) -> None:
        if not self.risk:
            self.risk = CircuitBreaker(starting_capital=self.capital)
        self.risk.record_trade_result(Decimal(str(-abs(amount))))
        self.capital = self.risk.current_capital

    async def check_circuit_breaker(self) -> None:
        if not self.risk:
            self.risk = CircuitBreaker(starting_capital=self.capital)
        self.risk.check_before_trade(Decimal("0.01"), self.capital)

    async def _execute_opportunity(self, opportunity: Dict) -> None:
        if not opportunity or not self.executor or not self.ledger or not self.risk:
            return

        if self.charlie is not None:
            charlie_agrees = await self.charlie.should_trade(opportunity)
            if not charlie_agrees:
                logger.info("charlie_veto_trade | market_id=%s", opportunity.get("market_id"))
                return

        equity = await self.ledger.get_equity()
        if equity <= 0:
            logger.warning("equity_unavailable_skipping_trade")
            return

        bet_size = await self.executor.calculate_bet_size(opportunity, equity)
        logger.info(
            "position_sized | equity=%s bet_size=%s",
            str(equity),
            str(bet_size),
        )
        self.risk.check_before_trade(bet_size, equity)
        order = await self.executor.execute_trade(opportunity, equity, bet_size=bet_size)

        if order and order.get("success"):
            logger.info("trade_executed | order_id=%s", order.get("order_id"))
            new_equity = await self.ledger.get_equity()
            self.risk.update_capital(new_equity)
            self.capital = new_equity

    async def run_trading_loop(self) -> None:
        """Main trading loop - scans for opportunities and executes trades."""
        logger.info("trading_loop_started")
        self.is_running = True

        while self.is_running:
            try:
                if self.price_subscriber:
                    btc_price = self.price_subscriber.get_price()
                    if not btc_price:
                        logger.warning("stale_price_data_skipping_iteration")
                        await asyncio.sleep(to_timeout_float(Decimal("5")))
                        continue

                    if self.price_history:
                        self.price_history.record_price("BTC", btc_price)

                if self.intelligence_subscriber:
                    intel = self.intelligence_subscriber.get_intelligence()
                    if intel:
                        logger.info(
                            "intel_update | lstm=%s whale=%s",
                            intel.get("lstm_prediction"),
                            intel.get("whale_flow"),
                        )

                opportunity = await self.scan_once()
                if opportunity:
                    logger.info(
                        "opportunity_detected | market_id=%s edge=%s side=%s",
                        opportunity.get("market_id"),
                        str(opportunity.get("edge")),
                        opportunity.get("side"),
                    )
                    await self._execute_opportunity(opportunity)
            except Exception as exc:
                logger.error("trading_loop_error | error=%s", str(exc))
                await asyncio.sleep(5)

            await asyncio.sleep(to_timeout_float(self.scan_interval))

    async def run_forever(self) -> None:
        logger.info("capital_doubler_started")
        await self.run_trading_loop()
        logger.info("capital_doubler_stopped")

    async def run_for_duration(self, seconds: int) -> None:
        """Run bot for specified duration."""
        self.is_running = True
        end_time = asyncio.get_running_loop().time() + seconds

        trading_task = asyncio.create_task(self.run_trading_loop())
        try:
            while asyncio.get_running_loop().time() < end_time:
                await asyncio.sleep(1)
        finally:
            self.is_running = False
            trading_task.cancel()
            logger.info("duration_complete | duration=%s", seconds)
            try:
                await trading_task
            except asyncio.CancelledError:
                pass


async def main() -> None:
    args = parse_args()

    logging.basicConfig(level=logging.INFO)
    settings.log_config()

    if args.debug:
        try:
            import structlog

            structlog.configure(
                wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            )
        except Exception:
            logging.getLogger().setLevel(logging.DEBUG)

    logger.info(
        "capital_doubler_starting | mode=%s capital=%s duration=%s",
        args.mode,
        args.capital,
        args.duration,
    )

    bot = CapitalDoublerBot(
        starting_capital=Decimal(str(args.capital)),
        mode=args.mode,
        config={"no_charlie": args.no_charlie},
    )

    logger.info("initializing_zmq_price_feed")
    price_subscriber = ZMQPriceSubscriber(
        connect_address="tcp://127.0.0.1:5555",
        staleness_threshold=1.0,
    )

    logger.info("initializing_redis_intelligence_feed")
    intelligence_subscriber = RedisIntelligenceSubscriber()

    asyncio.create_task(price_subscriber.start_listening())
    logger.info("zmq_subscriber_listening | port=5555")

    await asyncio.sleep(to_timeout_float(Decimal("2")))
    first_price = price_subscriber.get_price()
    if first_price:
        logger.info("first_btc_price_received | price=%s", first_price)
    else:
        logger.warning("no_price_received_is_charlie_running")

    bot.set_ipc_subscribers(price_subscriber, intelligence_subscriber)

    if args.duration:
        await bot.run_for_duration(seconds=args.duration)
    else:
        await bot.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("capital_doubler_stopped | reason=user_interrupt")
        sys.exit(0)
    except Exception as exc:
        logger.error("capital_doubler_crashed | error=%s", str(exc))
        sys.exit(1)
