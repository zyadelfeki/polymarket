#!/usr/bin/env python3
"""
Polymarket Latency Arbitrage Bot - Production Entry Point

This is the main production executable. It:
1. Initializes database and ledger
2. Sets up API clients
3. Configures risk management
4. Starts the latency arbitrage strategy
5. Monitors health and performance

Usage:
    python main_v2.py --mode paper --capital 10000
    python main_v2.py --mode live
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

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from database.ledger_async import AsyncLedger
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from risk.circuit_breaker_v2 import CircuitBreakerV2
from strategies.latency_arbitrage import LatencyArbitrageEngine

# Configure structured logging
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
    """
    Main trading bot orchestrator.
    
    Manages lifecycle of all components and handles graceful shutdown.
    """
    
    def __init__(self, config: dict):
        """
        Initialize bot.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.running = False
        
        # Components (initialized in start())
        self.ledger: AsyncLedger = None
        self.polymarket_client: PolymarketClientV2 = None
        self.execution_service: ExecutionServiceV2 = None
        self.circuit_breaker: CircuitBreakerV2 = None
        self.strategy: LatencyArbitrageEngine = None
        
        # Health monitoring
        self._health_task = None
        self._stats_task = None
    
    async def initialize(self):
        """
        Initialize all components.
        
        Order matters:
        1. Ledger (database)
        2. API Client
        3. Execution Service
        4. Circuit Breaker
        5. Strategy
        """
        logger.info("="*60)
        logger.info("POLYMARKET LATENCY ARBITRAGE BOT V2")
        logger.info("="*60)
        logger.info("")
        logger.info("initializing_bot", mode=self.config['mode'])
        
        # 1. Initialize Ledger
        logger.info("[1/5] Initializing database ledger...")
        
        db_path = self.config.get('db_path', 'data/trading.db')
        if self.config['mode'] == 'paper':
            db_path = ':memory:'  # Use in-memory for paper trading
        
        self.ledger = AsyncLedger(
            db_path=db_path,
            pool_size=10,
            cache_ttl=5
        )
        
        # CRITICAL: Initialize schema BEFORE any database operations
        await self.ledger.initialize()
        
        # Check if we need to seed capital
        equity = await self.ledger.get_equity()
        if equity == 0:
            initial_capital = Decimal(str(self.config.get('initial_capital', 10000)))
            await self.ledger.record_deposit(
                initial_capital,
                f"Initial capital - {self.config['mode']} mode"
            )
            equity = initial_capital
            logger.info(
                "initial_capital_deposited",
                amount=float(initial_capital)
            )
        
        logger.info(
            "ledger_initialized",
            db_path=db_path,
            equity=float(equity)
        )
        
        # 2. Initialize Polymarket Client
        logger.info("[2/5] Initializing Polymarket API client...")
        
        private_key = None
        if self.config['mode'] == 'live':
            private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
            if not private_key:
                raise ValueError("POLYMARKET_PRIVATE_KEY environment variable required for live trading")
        
        self.polymarket_client = PolymarketClientV2(
            private_key=private_key,
            paper_trading=(self.config['mode'] == 'paper'),
            rate_limit=10.0
        )
        
        logger.info(
            "polymarket_client_initialized",
            mode=self.config['mode'],
            can_trade=self.polymarket_client.can_trade
        )
        
        # 3. Initialize Execution Service
        logger.info("[3/5] Initializing execution service...")
        
        self.execution_service = ExecutionServiceV2(
            polymarket_client=self.polymarket_client,
            ledger=self.ledger,
            config={
                'max_retries': 3,
                'timeout_seconds': 30
            }
        )
        
        await self.execution_service.start()
        
        logger.info("execution_service_initialized")
        
        # 4. Initialize Circuit Breaker
        logger.info("[4/5] Initializing circuit breaker...")
        
        self.circuit_breaker = CircuitBreakerV2(
            initial_equity=equity,
            max_drawdown_pct=self.config.get('max_drawdown_pct', 15.0),
            max_loss_streak=self.config.get('max_loss_streak', 5),
            daily_loss_limit_pct=self.config.get('daily_loss_limit_pct', 10.0)
        )
        
        logger.info(
            "circuit_breaker_initialized",
            max_drawdown_pct=self.config.get('max_drawdown_pct', 15.0),
            max_loss_streak=self.config.get('max_loss_streak', 5)
        )
        
        # 5. Initialize Strategy
        logger.info("[5/5] Initializing latency arbitrage strategy...")
        
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
        
        logger.info("strategy_initialized")
        
        logger.info("")
        logger.info("="*60)
        logger.info("ALL COMPONENTS INITIALIZED SUCCESSFULLY")
        logger.info("="*60)
        logger.info("")
    
    async def start(self):
        """Start bot execution."""
        self.running = True
        
        logger.info("starting_bot")
        logger.info(
            "configuration",
            mode=self.config['mode'],
            market=self.config.get('market_id', 'btc_to_100k'),
            min_spread_bps=self.config.get('min_spread_bps', 50),
            max_position_pct=self.config.get('max_position_pct', 10.0)
        )
        
        # Start health monitoring
        self._health_task = asyncio.create_task(self._health_monitor())
        self._stats_task = asyncio.create_task(self._stats_reporter())
        
        # Start strategy
        try:
            await self.strategy.start()
        
        except asyncio.CancelledError:
            logger.info("bot_cancelled")
        
        except Exception as e:
            logger.error(
                "bot_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            raise
    
    async def stop(self):
        """Stop bot execution."""
        logger.info("stopping_bot")
        
        self.running = False
        
        # Stop health monitoring
        if self._health_task:
            self._health_task.cancel()
        if self._stats_task:
            self._stats_task.cancel()
        
        # Stop strategy
        if self.strategy:
            await self.strategy.stop()
        
        # Stop execution service
        if self.execution_service:
            await self.execution_service.stop()
        
        # Close API client
        if self.polymarket_client:
            await self.polymarket_client.close()
        
        # Close ledger
        if self.ledger:
            await self.ledger.close()
        
        logger.info("bot_stopped")
    
    async def _health_monitor(self):
        """Monitor system health."""
        while self.running:
            try:
                await asyncio.sleep(60)  # Every minute
                
                # Check circuit breaker
                cb_status = self.circuit_breaker.get_status()
                
                if cb_status['state'] == 'OPEN':
                    logger.warning(
                        "circuit_breaker_open",
                        reason=cb_status.get('reason', 'unknown')
                    )
                
                # Check equity
                equity = await self.ledger.get_equity()
                
                logger.info(
                    "health_check",
                    equity=float(equity),
                    circuit_breaker_state=cb_status['state']
                )
            
            except asyncio.CancelledError:
                break
            
            except Exception as e:
                logger.error(
                    "health_monitor_error",
                    error=str(e)
                )
    
    async def _stats_reporter(self):
        """Report statistics periodically."""
        while self.running:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                
                # Get metrics
                strategy_metrics = self.strategy.get_metrics()
                execution_metrics = self.execution_service.get_metrics()
                ledger_metrics = await self.ledger.get_metrics()
                
                # Get positions
                positions = await self.ledger.get_open_positions()
                
                logger.info(
                    "performance_report",
                    timestamp=datetime.utcnow().isoformat(),
                    strategy_signals=strategy_metrics['signals_generated'],
                    strategy_trades=strategy_metrics['trades_executed'],
                    strategy_execution_rate=strategy_metrics['execution_rate'],
                    execution_fill_rate=execution_metrics['fill_rate'],
                    execution_avg_latency_ms=execution_metrics['avg_execution_time_ms'],
                    open_positions=len(positions),
                    total_fees=execution_metrics['total_fees']
                )
            
            except asyncio.CancelledError:
                break
            
            except Exception as e:
                logger.error(
                    "stats_reporter_error",
                    error=str(e)
                )


async def main():
    """
    Main entry point.
    """
    # Parse arguments
    parser = argparse.ArgumentParser(description='Polymarket Latency Arbitrage Bot')
    parser.add_argument(
        '--mode',
        choices=['paper', 'live'],
        default='paper',
        help='Trading mode (paper or live)'
    )
    parser.add_argument(
        '--capital',
        type=float,
        default=10000,
        help='Initial capital (only for paper trading)'
    )
    parser.add_argument(
        '--market',
        default='btc_to_100k',
        help='Market ID to trade'
    )
    parser.add_argument(
        '--min-spread',
        type=int,
        default=50,
        help='Minimum spread in basis points (default: 50 = 0.5%%)'
    )
    parser.add_argument(
        '--max-position',
        type=float,
        default=10.0,
        help='Maximum position size as %% of equity (default: 10%%)'
    )
    
    args = parser.parse_args()
    
    # Build config
    config = {
        'mode': args.mode,
        'initial_capital': args.capital,
        'market_id': args.market,
        'min_spread_bps': args.min_spread,
        'max_position_pct': args.max_position,
        'db_path': 'data/trading.db' if args.mode == 'live' else ':memory:'
    }
    
    # Create bot
    bot = TradingBot(config)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("shutdown_signal_received", signal=sig)
        asyncio.create_task(bot.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Initialize
        await bot.initialize()
        
        # Start
        await bot.start()
    
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt_received")
    
    except Exception as e:
        logger.error(
            "fatal_error",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )
        raise
    
    finally:
        # Cleanup
        await bot.stop()
        
        # Print final summary
        logger.info("")
        logger.info("="*60)
        logger.info("BOT SHUTDOWN COMPLETE")
        logger.info("="*60)


if __name__ == '__main__':
    print("\n" + "="*60)
    print("POLYMARKET LATENCY ARBITRAGE BOT V2")
    print("="*60)
    print("\nInitializing...\n")
    
    try:
        asyncio.run(main())
    
    except KeyboardInterrupt:
        print("\n\nShutdown complete.")
    
    except Exception as e:
        print(f"\n\nFATAL ERROR: {e}")
        sys.exit(1)
    
    print("\n" + "="*60)
    print("EXECUTION COMPLETE")
    print("="*60 + "\n")
