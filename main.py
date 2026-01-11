#!/usr/bin/env python3
"""
Polymarket Trading Bot - Main Application

Production-grade trading system with:
- Multiple trading strategies
- Real-time market data
- Risk management
- Health monitoring
- Paper trading mode

Usage:
    python main.py --config config/production.yaml --mode paper
    python main.py --config config/production.yaml --mode live
"""

import asyncio
import signal
import sys
import argparse
from pathlib import Path
from typing import Optional
from decimal import Decimal
import yaml
import structlog

# Import core components
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from database.ledger_async import AsyncLedger
from services.execution_service_v2 import ExecutionServiceV2
from services.health_monitor_v2 import HealthMonitorV2
from risk.circuit_breaker_v2 import CircuitBreakerV2
from security.secrets_manager import SecretsManager, get_secrets_manager
from validation.models import TradingConfig

logger = structlog.get_logger(__name__)


class TradingSystem:
    """
    Main trading system orchestrator.
    
    Manages all components and coordinates trading operations.
    """
    
    def __init__(self, config: dict):
        """
        Initialize trading system.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.running = False
        self.shutdown_event = asyncio.Event()
        
        # Components (initialized in start())
        self.secrets_manager: Optional[SecretsManager] = None
        self.ledger: Optional[AsyncLedger] = None
        self.api_client: Optional[PolymarketClientV2] = None
        self.websocket: Optional[BinanceWebSocketV2] = None
        self.execution: Optional[ExecutionServiceV2] = None
        self.health_monitor: Optional[HealthMonitorV2] = None
        self.circuit_breaker: Optional[CircuitBreakerV2] = None
        
        logger.info(
            "trading_system_initialized",
            environment=config.get('environment', 'unknown'),
            paper_trading=config.get('trading', {}).get('paper_trading', True)
        )
    
    async def initialize_components(self):
        """Initialize all system components."""
        logger.info("initializing_components")
        
        try:
            # 1. Secrets Manager
            secrets_config = self.config.get('secrets', {})
            self.secrets_manager = SecretsManager(
                backend=secrets_config.get('backend', 'env'),
                aws_region=secrets_config.get('aws_region', 'us-east-1'),
                local_secrets_path=secrets_config.get('local_secrets_path', '.secrets.enc')
            )
            logger.info("secrets_manager_initialized")
            
            # 2. Database/Ledger
            db_config = self.config.get('database', {})
            db_path = db_config.get('path', 'data/trading.db')
            
            # Ensure directory exists
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            
            self.ledger = AsyncLedger(
                db_path=db_path,
                pool_size=db_config.get('pool_size', 5),
                cache_ttl=db_config.get('cache_ttl_seconds', 5)
            )
            await self.ledger.pool.initialize()
            logger.info("ledger_initialized", path=db_path)
            
            # Initialize with capital if needed
            equity = await self.ledger.get_equity()
            if equity == Decimal('0'):
                initial_capital = Decimal(str(self.config.get('trading', {}).get('initial_capital', 10000)))
                await self.ledger.record_deposit(initial_capital, "Initial capital")
                logger.info("initial_capital_deposited", amount=initial_capital)
            
            # 3. API Client
            api_config = self.config.get('api', {}).get('polymarket', {})
            paper_trading = self.config.get('trading', {}).get('paper_trading', True)
            
            # Get API credentials from secrets
            api_key = await self.secrets_manager.get_secret('polymarket_api_key')
            private_key = await self.secrets_manager.get_secret('polymarket_private_key')
            
            self.api_client = PolymarketClientV2(
                api_key=api_key,
                private_key=private_key,
                paper_trading=paper_trading,
                rate_limit=api_config.get('rate_limit', 8.0),
                timeout=api_config.get('timeout_seconds', 10.0),
                max_retries=api_config.get('max_retries', 3)
            )
            logger.info("api_client_initialized", paper_trading=paper_trading)
            
            # 4. WebSocket
            ws_config = self.config.get('api', {}).get('binance', {})
            symbols = self.config.get('markets', {}).get('crypto_symbols', ['BTC', 'ETH'])
            
            self.websocket = BinanceWebSocketV2(
                symbols=symbols,
                ws_url=ws_config.get('ws_url', 'wss://stream.binance.com:9443/ws'),
                heartbeat_interval=ws_config.get('heartbeat_interval', 30.0)
            )
            await self.websocket.start()
            logger.info("websocket_initialized", symbols=symbols)
            
            # 5. Circuit Breaker
            risk_config = self.config.get('risk', {})
            current_equity = await self.ledger.get_equity()
            
            self.circuit_breaker = CircuitBreakerV2(
                initial_equity=current_equity,
                max_drawdown_pct=risk_config.get('max_drawdown_pct', 15.0),
                max_loss_streak=risk_config.get('max_loss_streak', 5),
                daily_loss_limit_pct=risk_config.get('daily_loss_limit_pct', 10.0)
            )
            logger.info("circuit_breaker_initialized", initial_equity=current_equity)
            
            # 6. Execution Service
            exec_config = self.config.get('execution', {})
            
            self.execution = ExecutionServiceV2(
                api_client=self.api_client,
                ledger=self.ledger,
                order_timeout=exec_config.get('order_timeout_seconds', 60)
            )
            logger.info("execution_service_initialized")
            
            # 7. Health Monitor
            monitor_config = self.config.get('monitoring', {})
            
            self.health_monitor = HealthMonitorV2(
                check_interval=monitor_config.get('health_check_interval', 30.0),
                failure_threshold=monitor_config.get('failure_threshold', 3),
                auto_restart=monitor_config.get('auto_restart_enabled', True)
            )
            
            # Register components for health checks
            self.health_monitor.register_component(
                'api_client',
                self.api_client.health_check
            )
            self.health_monitor.register_component(
                'websocket',
                self.websocket.health_check
            )
            self.health_monitor.register_component(
                'database',
                self._check_database_health
            )
            
            await self.health_monitor.start()
            logger.info("health_monitor_initialized")
            
            logger.info("all_components_initialized")
            
        except Exception as e:
            logger.error(
                "component_initialization_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise
    
    async def _check_database_health(self) -> bool:
        """Check database health."""
        try:
            if not self.ledger:
                return False
            
            # Simple query to verify connection
            equity = await self.ledger.get_equity()
            return equity is not None
        except Exception:
            return False
    
    async def start(self):
        """Start the trading system."""
        logger.info("starting_trading_system")
        
        try:
            # Initialize all components
            await self.initialize_components()
            
            self.running = True
            
            logger.info(
                "trading_system_started",
                status="operational"
            )
            
            # Main loop
            await self._main_loop()
            
        except Exception as e:
            logger.error(
                "trading_system_start_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise
    
    async def _main_loop(self):
        """Main trading loop."""
        logger.info("entering_main_loop")
        
        iteration = 0
        
        while self.running:
            try:
                iteration += 1
                
                # Wait for shutdown or next iteration
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=60.0  # Check every minute
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Continue normal operation
                
                # Periodic tasks
                if iteration % 1 == 0:  # Every minute
                    await self._periodic_check()
                
                if iteration % 5 == 0:  # Every 5 minutes
                    await self._periodic_maintenance()
                
            except Exception as e:
                logger.error(
                    "main_loop_error",
                    iteration=iteration,
                    error=str(e),
                    error_type=type(e).__name__
                )
                
                # Decide whether to continue
                if self.config.get('safety', {}).get('emergency_stop_on_error', True):
                    logger.critical("emergency_stop_triggered")
                    break
                
                # Otherwise, continue after delay
                await asyncio.sleep(10)
        
        logger.info("exiting_main_loop")
    
    async def _periodic_check(self):
        """Periodic health and status check."""
        try:
            # Check circuit breaker
            if self.circuit_breaker:
                equity = await self.ledger.get_equity()
                can_trade = await self.circuit_breaker.can_trade(equity)
                
                if not can_trade:
                    logger.warning(
                        "circuit_breaker_open",
                        status=self.circuit_breaker.get_status()
                    )
            
            # Log system status
            logger.info(
                "periodic_status_check",
                equity=float(await self.ledger.get_equity()) if self.ledger else 0,
                api_healthy=await self.api_client.health_check() if self.api_client else False,
                ws_connected=self.websocket.connected if self.websocket else False,
                circuit_breaker_state=self.circuit_breaker.state.value if self.circuit_breaker else 'unknown'
            )
            
        except Exception as e:
            logger.error(
                "periodic_check_failed",
                error=str(e)
            )
    
    async def _periodic_maintenance(self):
        """Periodic maintenance tasks."""
        try:
            # Validate ledger
            if self.ledger:
                is_balanced = await self.ledger.validate_ledger()
                if not is_balanced:
                    logger.error("ledger_validation_failed", message="Ledger not balanced!")
            
            # Clean up execution service
            if self.execution:
                cleaned = await self.execution.cleanup_old_orders(max_age_seconds=3600)
                if cleaned > 0:
                    logger.info("orders_cleaned_up", count=cleaned)
            
            # Clear cache
            if self.secrets_manager:
                self.secrets_manager.clear_cache()
            
        except Exception as e:
            logger.error(
                "periodic_maintenance_failed",
                error=str(e)
            )
    
    async def stop(self):
        """Stop the trading system."""
        logger.info("stopping_trading_system")
        
        self.running = False
        self.shutdown_event.set()
        
        try:
            # Stop health monitor
            if self.health_monitor:
                await self.health_monitor.stop()
                logger.info("health_monitor_stopped")
            
            # Stop WebSocket
            if self.websocket:
                await self.websocket.stop()
                logger.info("websocket_stopped")
            
            # Close API client
            if self.api_client:
                await self.api_client.close()
                logger.info("api_client_closed")
            
            # Close ledger
            if self.ledger:
                await self.ledger.close()
                logger.info("ledger_closed")
            
            logger.info("trading_system_stopped")
            
        except Exception as e:
            logger.error(
                "shutdown_error",
                error=str(e),
                error_type=type(e).__name__
            )


async def main():
    """Main entry point."""
    # Parse arguments
    parser = argparse.ArgumentParser(description='Polymarket Trading Bot')
    parser.add_argument(
        '--config',
        default='config/production.yaml',
        help='Configuration file path'
    )
    parser.add_argument(
        '--mode',
        choices=['paper', 'live'],
        default='paper',
        help='Trading mode'
    )
    args = parser.parse_args()
    
    # Configure logging
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("config_not_found", path=str(config_path))
        sys.exit(1)
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    # Override paper trading mode from command line
    if 'trading' not in config:
        config['trading'] = {}
    config['trading']['paper_trading'] = (args.mode == 'paper')
    
    logger.info(
        "configuration_loaded",
        config_path=str(config_path),
        mode=args.mode,
        paper_trading=config['trading']['paper_trading']
    )
    
    # Create trading system
    system = TradingSystem(config)
    
    # Set up signal handlers
    def signal_handler(signum, frame):
        logger.info("shutdown_signal_received", signal=signum)
        asyncio.create_task(system.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start system
    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    except Exception as e:
        logger.error(
            "fatal_error",
            error=str(e),
            error_type=type(e).__name__
        )
        sys.exit(1)
    finally:
        await system.stop()


if __name__ == '__main__':
    asyncio.run(main())
