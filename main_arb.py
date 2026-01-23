import asyncio
import logging
from decimal import Decimal
from typing import Optional
import logging.config

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from data_feeds.kalshi_client_v1 import KalshiClient
from data_feeds.news_monitor_v1 import NewsMonitor
from strategies.arb_engine_v1 import ArbitrageEngine
from strategies.sentiment_arb_v1 import NewsArbitrage
from strategies.market_maker_v1 import MarketMaker
from database.ledger_async import AsyncLedger
from services.execution_service_v2 import ExecutionServiceV2
from risk.kelly_sizer import AdaptiveKellySizer


logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "[%(asctime)s] %(name)s - %(levelname)s - %(message)s"
        },
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
        "file": {
            "level": "DEBUG",
            "class": "logging.FileHandler",
            "filename": "logs/arb_trading.log",
            "formatter": "standard",
        }
    },
    "loggers": {
        "": {
            "handlers": ["default", "file"],
            "level": "DEBUG",
        }
    }
})

logger = logging.getLogger(__name__)


class ArbitrageBot:
    """Main orchestrator for cross-platform arbitrage + news + market making."""

    def __init__(self, config: dict):
        self.config = config
        self.poly_client: Optional[PolymarketClientV2] = None
        self.kalshi_client: Optional[KalshiClient] = None
        self.arb_engine: Optional[ArbitrageEngine] = None
        self.news_monitor: Optional[NewsMonitor] = None
        self.news_arb: Optional[NewsArbitrage] = None
        self.market_maker: Optional[MarketMaker] = None
        self.ledger: Optional[AsyncLedger] = None
        self.execution_service: Optional[ExecutionServiceV2] = None
        self.kelly_sizer: Optional[AdaptiveKellySizer] = None

        self.running = False
        self.scan_interval = config.get("scan_interval_seconds", 5)
        self.min_profit_pct = Decimal(str(config.get("min_profit_pct", 2.0)))

    async def initialize(self):
        """Initialize all components."""
        logger.info("🚀 Initializing Arbitrage Bot...")

        self.poly_client = PolymarketClientV2(
            private_key=self.config.get("POLYMARKET_PRIVATE_KEY"),
            api_key=self.config.get("POLYMARKET_API_KEY"),
            paper_trading=self.config.get("paper_trading", True)
        )

        self.kalshi_client = KalshiClient(
            api_key=self.config.get("KALSHI_API_KEY"),
            api_secret=self.config.get("KALSHI_API_SECRET"),
            paper=self.config.get("paper_trading", True)
        )
        await self.kalshi_client.initialize()

        self.ledger = AsyncLedger(db_path=self.config.get("db_path", "trading.db"))
        await self.ledger.initialize()

        self.execution_service = ExecutionServiceV2(self.poly_client, self.ledger)

        self.kelly_sizer = AdaptiveKellySizer(config={
            "kelly_fraction": 0.25,
            "min_edge": 0.02,
            "max_bet_pct": 5.0,
        })

        self.arb_engine = ArbitrageEngine(
            self.poly_client,
            self.kalshi_client,
            config={
                "min_profit_pct": float(self.min_profit_pct),
                "max_position_pct": 15.0,
                "min_trade_size": 10.0,
                "max_trade_size": 1000.0,
            }
        )

        self.news_monitor = NewsMonitor(self.config)
        await self.news_monitor.initialize()

        self.news_arb = NewsArbitrage(
            self.poly_client,
            self.kelly_sizer,
            config={"min_confidence": 0.70}
        )
        await self.news_monitor.register_callback(self.news_arb.process_news_alert)

        self.market_maker = MarketMaker(
            self.poly_client,
            config={
                "max_inventory_pct": 10.0,
                "target_spread_pct": 4.0,
                "max_markets": 5,
            }
        )

        logger.info("✅ All components initialized")

    async def scan_and_execute(self):
        """Main trading loop."""
        scan_count = 0

        asyncio.create_task(self.news_monitor.run_monitoring_loop())
        asyncio.create_task(self.market_maker.run_market_making_loop())

        while self.running:
            try:
                scan_count += 1

                equity = await self.ledger.get_equity()
                await self.arb_engine.update_equity(equity)

                logger.info(f"\n📊 Scan #{scan_count} | Equity: ${equity:.2f}")

                opportunities = await self.arb_engine.scan_opportunities()
                logger.info(f"Found {len(opportunities)} opportunities")

                for opp in opportunities:
                    if not opp.is_fresh():
                        continue

                    try:
                        result = await self.arb_engine.execute_arbitrage(opp)
                        logger.info(f"✅ Trade executed: {result}")

                        await self.ledger.record_trade_entry(
                            order_id=result["buy_order_id"],
                            market_id=opp.market_id,
                            token_id="yes",
                            strategy="arb",
                            side="BUY",
                            quantity=Decimal(str(result["position_size"])),
                            price=opp.buy_price,
                            correlation_id=result["execution_id"]
                        )

                    except Exception as e:
                        logger.error(f"❌ Trade execution failed: {e}")
                        continue

                if self.news_arb:
                    await self.news_arb.check_exit_conditions()

                stats = self.arb_engine.get_stats()
                logger.info(
                    f"📈 Stats: Executed: {stats['opportunities_executed']}, "
                    f"Profit: ${stats['total_profit']:.2f}, "
                    f"Loss: ${stats['total_loss']:.2f}"
                )

                await asyncio.sleep(self.scan_interval)

            except KeyboardInterrupt:
                logger.info("\n⏹️  Shutting down...")
                break
            except Exception as e:
                logger.error(f"❌ Scan loop error: {e}")
                await asyncio.sleep(5)

    async def shutdown(self):
        """Cleanup resources."""
        logger.info("Shutting down bot...")
        self.running = False

        if self.kalshi_client:
            await self.kalshi_client.close()
        if self.news_monitor:
            await self.news_monitor.close()
        if self.ledger:
            await self.ledger.close()

        logger.info("✅ Bot shutdown complete")

    async def run(self):
        """Main entry point."""
        try:
            await self.initialize()
            self.running = True
            await self.scan_and_execute()
        finally:
            await self.shutdown()


async def main():
    config = {
        "POLYMARKET_API_KEY": "YOUR_KEY",
        "POLYMARKET_PRIVATE_KEY": "YOUR_PRIVATE_KEY",
        "KALSHI_API_KEY": "YOUR_KEY",
        "KALSHI_API_SECRET": "YOUR_SECRET",
        "paper_trading": True,
        "db_path": "trading.db",
        "scan_interval_seconds": 5,
        "min_profit_pct": 2.0,
    }

    bot = ArbitrageBot(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
