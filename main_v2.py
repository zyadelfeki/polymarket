#!/usr/bin/env python3
"""
Polymarket Latency Arbitrage Bot - Production Entry Point
"""

import asyncio
import argparse
import signal
import sys
from pathlib import Path
from decimal import Decimal
from datetime import datetime
import structlog
import os
import logging
from dotenv import load_dotenv
from utils.correlation_id import CorrelationIdFilter, structlog_correlation_processor

# Load environment variables BEFORE any config initialization
load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).parent))

from database.ledger_async import AsyncLedger
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from risk.circuit_breaker_v2 import CircuitBreakerV2
from strategies.latency_arbitrage import LatencyArbitrageEngine

logger = structlog.get_logger(__name__)


class TradingBot:
    def __init__(self, config: dict):
        self.config = config
        self.running = False
        self.ledger: AsyncLedger = None
        self.polymarket_client: PolymarketClientV2 = None
        self.execution_service: ExecutionServiceV2 = None
        self.circuit_breaker: CircuitBreakerV2 = None
        self.strategy: LatencyArbitrageEngine = None
        self._health_task = None
        self._stats_task = None
        self._heartbeat_task = None
    
    async def initialize(self):
        logger.info("startup_banner", mode=self.config["mode"], capital=str(self.config["initial_capital"]), debug=self.config.get("debug", False))
        
        # STEP 1: Database
        logger.info("init_step", step="1/6", action="initializing_database")
        
        # CRITICAL: :memory: databases are per-connection in SQLite
        # For paper trading with connection pooling, use a temp file instead
        if self.config['mode'] == 'paper':
            db_path = 'file:memdb1?mode=memory&cache=shared'  # Shared memory DB
        else:
            db_path = self.config.get('db_path', 'data/trading.db')
        
        self.ledger = AsyncLedger(db_path=db_path, pool_size=10, cache_ttl=5)
        
        logger.info("database_schema_create_start")
        try:
            await self.ledger.initialize()
            logger.info("database_schema_created")
        except Exception as e:
            logger.error("database_schema_failed", error=str(e))
            raise
        
        # Verify tables exist
        logger.info("database_schema_verify_start")
        try:
            conn = await self.ledger.pool.acquire()
            try:
                cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = await cursor.fetchall()
                table_names = [t[0] for t in tables]
            logger.info("database_tables_found", tables=table_names)
                
                if 'transactions' not in table_names:
                    raise RuntimeError("transactions table missing after schema init!")
                if 'accounts' not in table_names:
                    raise RuntimeError("accounts table missing after schema init!")
            finally:
                await self.ledger.pool.release(conn)
        except Exception as e:
            logger.error("database_table_verification_failed", error=str(e))
            raise
        
        logger.info("ledger_initialized", db_path=db_path)
        
        # STEP 2: Client
        logger.info("init_step", step="2/6", action="initializing_api_client")
        private_key = None
        if self.config['mode'] == 'live':
            private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
            if not private_key:
                raise ValueError("POLYMARKET_PRIVATE_KEY required for live trading")
        
        self.polymarket_client = PolymarketClientV2(
            private_key=private_key,
            paper_trading=(self.config['mode'] == 'paper'),
            rate_limit=10.0
        )
        logger.info("client_ready", paper_trading=(self.config["mode"] == "paper"))
        
        # STEP 2.5: Sync wallet balance for live mode
        if self.config['mode'] == 'live':
            logger.info("init_step", step="2.5/6", action="sync_wallet_balance")
            try:
                wallet_balance = await self.polymarket_client.get_usdc_balance()
                logger.info("live_wallet_balance", amount=float(wallet_balance))
                logger.info("wallet_balance", amount=str(wallet_balance))
                
                # Check local ledger
                local_equity = await self.ledger.get_equity()
                logger.info("ledger_equity", amount=str(local_equity))
                
                if local_equity == 0 and wallet_balance > 0:
                    logger.info("wallet_sync_start", amount=str(wallet_balance))
                    await self.ledger.record_deposit(wallet_balance, "Initial Wallet Sync")
                    logger.info("wallet_sync_complete")
                elif abs(local_equity - wallet_balance) > Decimal('0.01'):
                    logger.warning(
                        "wallet_ledger_mismatch",
                        wallet_balance=float(wallet_balance),
                        ledger_equity=float(local_equity),
                        difference=float(abs(wallet_balance - local_equity))
                    )
                    logger.warning("wallet_ledger_mismatch_warning", difference=str(abs(wallet_balance - local_equity)))
                else:
                    logger.info("wallet_ledger_in_sync")
            except Exception as e:
                logger.error("wallet_sync_failed", error=str(e))
                logger.warning("wallet_sync_failed_warning", error=str(e))
        
        # STEP 3: Initial Capital
        logger.info("init_step", step="3/6", action="setting_up_capital")
        equity = await self.ledger.get_equity()
        logger.info("current_equity", amount=str(equity))
        
        if equity == 0 and self.config['mode'] == 'paper':
            initial_capital = Decimal(str(self.config['initial_capital']))
            logger.info("deposit_start", amount=str(initial_capital))
            await self.ledger.record_deposit(initial_capital, "Initial paper capital")
            equity = await self.ledger.get_equity()
            logger.info("deposit_complete", equity=str(equity))
        
        # STEP 4: Execution
        logger.info("init_step", step="4/6", action="initializing_execution_service")
        self.execution_service = ExecutionServiceV2(
            polymarket_client=self.polymarket_client,
            ledger=self.ledger,
            config={'max_retries': 3, 'timeout_seconds': 30}
        )
        await self.execution_service.start()
        logger.info("execution_service_ready")
        
        # STEP 5: Circuit Breaker
        logger.info("init_step", step="5/6", action="initializing_circuit_breaker")
        self.circuit_breaker = CircuitBreakerV2(
            initial_equity=equity,
            max_drawdown_pct=self.config.get('max_drawdown_pct', 15.0),
            max_loss_streak=self.config.get('max_loss_streak', 5),
            daily_loss_limit_pct=self.config.get('daily_loss_limit_pct', 10.0),
            audit_logger=self.ledger
        )
        logger.info("circuit_breaker_ready", max_drawdown_pct=self.config.get('max_drawdown_pct', 15.0))
        
        # STEP 6: Strategy
        logger.info("init_step", step="6/6", action="initializing_strategy")
        self.strategy = LatencyArbitrageEngine(
            ledger=self.ledger,
            polymarket_client=self.polymarket_client,
            execution_service=self.execution_service,
            circuit_breaker=self.circuit_breaker,
            config={
                # Market: "Will the price of Bitcoin be above $94,000 on January 13?"
                'market_id': self.config.get('market_id', '0xd3460cd313aa9759ea67a966e9a499cb65964d6e2a2ff6902472aa83005383bb'),
                'token_id': self.config.get('token_id', 'token_yes'),
                'min_spread_bps': self.config.get('min_spread_bps', 50),
                'max_spread_bps': self.config.get('max_spread_bps', 500),
                'max_position_pct': self.config.get('max_position_pct', 10.0),
                'poll_interval': self.config.get('poll_interval', 2.0),
                'btc_target': self.config.get('btc_target', 100000),
                'debug': self.config.get('debug', False)
            }
        )
        logger.info("strategy_ready")
        
        logger.info(
            "initialization_complete",
            equity=str(equity),
            min_spread_bps=self.config.get('min_spread_bps', 50),
            max_spread_bps=self.config.get('max_spread_bps', 500),
            max_position_pct=self.config.get('max_position_pct', 10.0)
        )
    
    async def start(self):
        self.running = True
        logger.info("starting_strategy")
        
        heartbeat_interval = 10 if self.config.get('debug', False) else 30
        self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor(heartbeat_interval))
        self._health_task = asyncio.create_task(self._health_monitor())
        self._stats_task = asyncio.create_task(self._stats_reporter())
        
        try:
            await self.strategy.start()
        except asyncio.CancelledError:
            logger.info("bot_cancelled")
        except Exception as e:
            logger.error("bot_error", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise
    
    async def stop(self):
        logger.info("stopping_bot")
        self.running = False
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._health_task:
            self._health_task.cancel()
        if self._stats_task:
            self._stats_task.cancel()
        if self.strategy:
            await self.strategy.stop()
        if self.execution_service:
            await self.execution_service.stop()
        if self.polymarket_client:
            await self.polymarket_client.close()
        if self.ledger:
            await self.ledger.close()
        
        logger.info("bot_stopped")
    
    async def _heartbeat_monitor(self, interval: int):
        """Periodic heartbeat to show the bot is alive"""
        while self.running:
            try:
                await asyncio.sleep(interval)
                
                equity = await self.ledger.get_equity()
                positions = await self.ledger.get_open_positions()
                
                metrics = self.strategy.get_metrics() if self.strategy else {}
                
                logger.info(
                    "heartbeat",
                    status="active",
                    equity=float(equity),
                    open_positions=len(positions),
                    signals=metrics.get('signals_generated', 0),
                    trades=metrics.get('trades_executed', 0)
                )
                
                if self.config.get('debug', False):
                    logger.debug(
                        "heartbeat_debug",
                        last_check=datetime.now().isoformat(),
                        circuit_breaker=self.circuit_breaker.get_status()['state']
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
    
    async def _health_monitor(self):
        while self.running:
            try:
                await asyncio.sleep(60)
                cb_status = self.circuit_breaker.get_status()
                if cb_status['state'] == 'OPEN':
                    logger.warning("circuit_breaker_open", reason=cb_status.get('reason', 'unknown'))
                equity = await self.ledger.get_equity()
                logger.info("health_check", equity=float(equity), circuit_breaker_state=cb_status['state'])
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("health_monitor_error", error=str(e))
    
    async def _stats_reporter(self):
        report_interval = 60 if self.config.get('debug', False) else 300
        
        while self.running:
            try:
                await asyncio.sleep(report_interval)
                strategy_metrics = self.strategy.get_metrics()
                execution_metrics = self.execution_service.get_metrics()
                positions = await self.ledger.get_open_positions()
                
                logger.info("")
                logger.info("="*60)
                logger.info("PERFORMANCE REPORT")
                logger.info("="*60)
                logger.info(f"Signals: {strategy_metrics['signals_generated']}")
                logger.info(f"Trades: {strategy_metrics['trades_executed']}")
                logger.info(f"Execution Rate: {strategy_metrics['execution_rate']:.2%}")
                logger.info(f"Avg Latency: {execution_metrics['avg_execution_time_ms']:.2f}ms")
                logger.info(f"Open Positions: {len(positions)}")
                logger.info("="*60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("stats_reporter_error", error=str(e))


async def main():
    parser = argparse.ArgumentParser(description='Polymarket Latency Arbitrage Bot')
    parser.add_argument('--mode', choices=['paper', 'live'], default='paper')
    parser.add_argument('--capital', type=str, default="10000")
    # Market: "Will the price of Bitcoin be above $94,000 on January 13?"
    parser.add_argument('--market', default='0xd3460cd313aa9759ea67a966e9a499cb65964d6e2a2ff6902472aa83005383bb')
    parser.add_argument('--min-spread', type=int, default=50)
    parser.add_argument('--max-spread', type=int, default=500, help="Maximum spread in bps (safety cap)")
    parser.add_argument('--max-position', type=float, default=10.0)
    parser.add_argument('--debug', action='store_true', help="Enable verbose debug logs")
    args = parser.parse_args()
    
    # Configure logging level
    log_level = logging.DEBUG if args.debug else logging.INFO
    
    structlog.configure(
        processors=[
            structlog_correlation_processor,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s | %(levelname)s | %(name)s | %(correlation_id)s | %(message)s'
    )
    logging.getLogger().addFilter(CorrelationIdFilter())
    
    config = {
        'mode': args.mode,
        'initial_capital': Decimal(str(args.capital)),
        'market_id': args.market,
        'min_spread_bps': args.min_spread,
        'max_spread_bps': args.max_spread,
        'max_position_pct': args.max_position,
        'debug': args.debug,
        'db_path': 'data/trading.db' if args.mode == 'live' else ':memory:'
    }
    
    bot = TradingBot(config)
    
    # Signal handler must be async-aware
    loop = asyncio.get_event_loop()
    
    def signal_handler(sig, frame):
        """Handle shutdown signals gracefully."""
        logger.info("shutdown_signal_received", signal=sig)
        # Create task in the running event loop
        loop.create_task(bot.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await bot.initialize()
        await bot.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    except Exception as e:
        logger.error("fatal_error", error=str(e), error_type=type(e).__name__, exc_info=True)
        raise
    finally:
        await bot.stop()
        logger.info("shutdown_complete")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("fatal_startup_error", error=str(e))
        sys.exit(1)
