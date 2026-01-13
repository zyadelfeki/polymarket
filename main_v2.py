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

sys.path.insert(0, str(Path(__file__).parent))

from database.ledger_async import AsyncLedger
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from risk.circuit_breaker_v2 import CircuitBreakerV2
from strategies.latency_arbitrage import LatencyArbitrageEngine

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

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
    
    async def initialize(self):
        print("\n" + "="*60)
        print("POLYMARKET LATENCY ARBITRAGE BOT V2")
        print("="*60)
        print(f"Mode: {self.config['mode']}")
        print(f"Capital: ${self.config['initial_capital']:.2f}")
        print("="*60 + "\n")
        
        # STEP 1: Database
        print("[1/6] Initializing database...")
        
        # CRITICAL: :memory: databases are per-connection in SQLite
        # For paper trading with connection pooling, use a temp file instead
        if self.config['mode'] == 'paper':
            db_path = 'file:memdb1?mode=memory&cache=shared'  # Shared memory DB
        else:
            db_path = self.config.get('db_path', 'data/trading.db')
        
        self.ledger = AsyncLedger(db_path=db_path, pool_size=10, cache_ttl=5)
        
        print("  - Creating database schema...")
        try:
            await self.ledger.initialize()
            print("  ✓ Schema created")
        except Exception as e:
            print(f"  ✗ Schema creation FAILED: {e}")
            raise
        
        # Verify tables exist
        print("  - Verifying tables...")
        try:
            conn = await self.ledger.pool.acquire()
            try:
                cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = await cursor.fetchall()
                table_names = [t[0] for t in tables]
                print(f"  ✓ Tables found: {', '.join(table_names)}")
                
                if 'transactions' not in table_names:
                    raise RuntimeError("transactions table missing after schema init!")
                if 'accounts' not in table_names:
                    raise RuntimeError("accounts table missing after schema init!")
            finally:
                await self.ledger.pool.release(conn)
        except Exception as e:
            print(f"  ✗ Table verification FAILED: {e}")
            raise
        
        logger.info("ledger_initialized", db_path=db_path)
        
        # STEP 2: Client
        print("\n[2/6] Initializing API client...")
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
        print(f"  ✓ Client ready (paper={self.config['mode'] == 'paper'})")
        
        # STEP 3: Initial Capital
        print("\n[3/6] Setting up capital...")
        equity = await self.ledger.get_equity()
        print(f"  - Current equity: ${float(equity):.2f}")
        
        if equity == 0 and self.config['mode'] == 'paper':
            initial_capital = Decimal(str(self.config['initial_capital']))
            print(f"  - Depositing ${float(initial_capital):.2f}...")
            await self.ledger.record_deposit(initial_capital, "Initial paper capital")
            equity = await self.ledger.get_equity()
            print(f"  ✓ Deposit complete, equity: ${float(equity):.2f}")
        
        # STEP 4: Execution
        print("\n[4/6] Initializing execution service...")
        self.execution_service = ExecutionServiceV2(
            polymarket_client=self.polymarket_client,
            ledger=self.ledger,
            config={'max_retries': 3, 'timeout_seconds': 30}
        )
        await self.execution_service.start()
        print("  ✓ Execution service ready")
        
        # STEP 5: Circuit Breaker
        print("\n[5/6] Initializing circuit breaker...")
        self.circuit_breaker = CircuitBreakerV2(
            initial_equity=equity,
            max_drawdown_pct=self.config.get('max_drawdown_pct', 15.0),
            max_loss_streak=self.config.get('max_loss_streak', 5),
            daily_loss_limit_pct=self.config.get('daily_loss_limit_pct', 10.0)
        )
        print(f"  ✓ Circuit breaker ready (max drawdown: {self.config.get('max_drawdown_pct', 15.0)}%)")
        
        # STEP 6: Strategy
        print("\n[6/6] Initializing strategy...")
        self.strategy = LatencyArbitrageEngine(
            ledger=self.ledger,
            polymarket_client=self.polymarket_client,
            execution_service=self.execution_service,
            circuit_breaker=self.circuit_breaker,
            config={
                'market_id': self.config.get('market_id', 'btc_to_100k'),
                'token_id': self.config.get('token_id', 'token_yes'),
                'min_spread_bps': self.config.get('min_spread_bps', 50),
                'max_spread_bps': self.config.get('max_spread_bps', 500),
                'max_position_pct': self.config.get('max_position_pct', 10.0),
                'poll_interval': self.config.get('poll_interval', 2.0),
                'btc_target': self.config.get('btc_target', 100000)
            }
        )
        print("  ✓ Strategy ready")
        
        print("\n" + "="*60)
        print("INITIALIZATION COMPLETE")
        print("="*60)
        print(f"Equity: ${float(equity):.2f}")
        print(f"Min Spread: {self.config.get('min_spread_bps', 50)} bps")
        print(f"Max Spread: {self.config.get('max_spread_bps', 500)} bps")
        print(f"Max Position: {self.config.get('max_position_pct', 10.0)}%")
        print("="*60 + "\n")
    
    async def start(self):
        self.running = True
        print("Starting strategy...\n")
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
        while self.running:
            try:
                await asyncio.sleep(300)
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
    parser.add_argument('--capital', type=float, default=10000)
    parser.add_argument('--market', default='btc_to_100k')
    parser.add_argument('--min-spread', type=int, default=50)
    parser.add_argument('--max-spread', type=int, default=500, help="Maximum spread in bps (safety cap)")
    parser.add_argument('--max-position', type=float, default=10.0)
    args = parser.parse_args()
    
    config = {
        'mode': args.mode,
        'initial_capital': args.capital,
        'market_id': args.market,
        'min_spread_bps': args.min_spread,
        'max_spread_bps': args.max_spread,
        'max_position_pct': args.max_position,
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
        print("\nShutdown complete.")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nFATAL: {e}")
        sys.exit(1)
