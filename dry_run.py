#!/usr/bin/env python3
"""
Dry Run: Single Loop Execution

Shows actual logs from:
  Binance Tick -> Signal Generation -> Risk Check -> Execution -> Ledger

This is what the senior engineer wants to see.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
import sys
from pathlib import Path
import structlog

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger
from services.execution_service_v2 import ExecutionServiceV2
from risk.circuit_breaker_v2 import CircuitBreakerV2

# Configure structured logging to console
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S.%f", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer()  # Human-readable for dry run
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)


class DryRun:
    """
    Single execution loop demonstrating the full flow.
    """
    
    def __init__(self):
        self.ledger = None
        self.api_client = None
        self.execution = None
        self.circuit_breaker = None
        self.websocket = None
        
        self.tick_count = 0
        self.max_ticks = 3  # Process 3 ticks then stop
        self.test_complete = asyncio.Event()
    
    async def initialize(self):
        """Initialize all components."""
        logger.info("=" * 60)
        logger.info("DRY RUN INITIALIZATION")
        logger.info("=" * 60)
        
        # 1. Ledger (in-memory for testing)
        logger.info("Initializing ledger...")
        self.ledger = AsyncLedger(db_path=":memory:", pool_size=5)
        await self.ledger.pool.initialize()
        
        # Initial capital
        initial_capital = Decimal('10000')
        await self.ledger.record_deposit(initial_capital, "Initial capital")
        
        equity = await self.ledger.get_equity()
        logger.info(
            "ledger_initialized",
            initial_capital=float(initial_capital),
            current_equity=float(equity)
        )
        
        # 2. API Client
        logger.info("Initializing API client...")
        self.api_client = PolymarketClientV2(
            private_key=None,
            paper_trading=True,
            rate_limit=10.0
        )
        logger.info("api_client_initialized", paper_trading=True)
        
        # 3. Circuit Breaker
        logger.info("Initializing circuit breaker...")
        self.circuit_breaker = CircuitBreakerV2(
            initial_equity=equity,
            max_drawdown_pct=15.0,
            max_loss_streak=5,
            daily_loss_limit_pct=10.0
        )
        logger.info(
            "circuit_breaker_initialized",
            initial_equity=float(equity),
            max_drawdown_pct=15.0
        )
        
        # 4. Execution Service
        logger.info("Initializing execution service...")
        self.execution = ExecutionServiceV2(
            api_client=self.api_client,
            ledger=self.ledger
        )
        await self.execution.start()
        logger.info("execution_service_initialized")
        
        # 5. WebSocket with price callback
        logger.info("Initializing Binance WebSocket...")
        self.websocket = BinanceWebSocketV2(
            symbols=['BTC'],
            on_price_update=self._on_price_update
        )
        await self.websocket.start()
        logger.info("binance_websocket_connected", symbols=['BTC'])
        
        logger.info("=" * 60)
        logger.info("ALL COMPONENTS INITIALIZED")
        logger.info("=" * 60)
    
    async def _on_price_update(self, symbol: str, price_data):
        """
        Main strategy loop triggered by price updates.
        
        This demonstrates the full flow:
          1. Receive Binance tick
          2. Generate signal
          3. Check risk (circuit breaker)
          4. Execute order
          5. Record in ledger
        """
        if symbol != 'BTC':
            return
        
        self.tick_count += 1
        
        if self.tick_count > self.max_ticks:
            if not self.test_complete.is_set():
                self.test_complete.set()
            return
        
        logger.info("\n" + "=" * 60)
        logger.info(f"TICK #{self.tick_count} - BINANCE PRICE UPDATE")
        logger.info("=" * 60)
        
        # STEP 1: Binance Tick
        logger.info(
            "binance_price_received",
            symbol=symbol,
            price=float(price_data.price),
            timestamp=price_data.timestamp.isoformat()
        )
        
        try:
            # STEP 2: Signal Generation (simplified for dry run)
            # In reality, this would compare Binance price to Polymarket odds
            logger.info("\n--- SIGNAL GENERATION ---")
            
            # Simulate: BTC price dropped, we think "YES to 100K" odds should increase
            signal = {
                'action': 'BUY',
                'market_id': 'market_btc_100k',
                'token_id': 'token_yes',
                'side': 'YES',
                'confidence': 0.75,
                'target_price': Decimal('0.52'),
                'quantity': Decimal('50')
            }
            
            logger.info(
                "signal_generated",
                action=signal['action'],
                market=signal['market_id'],
                side=signal['side'],
                confidence=signal['confidence'],
                target_price=float(signal['target_price']),
                quantity=float(signal['quantity'])
            )
            
            # STEP 3: Risk Check - Circuit Breaker
            logger.info("\n--- RISK CHECK ---")
            
            current_equity = await self.ledger.get_equity()
            can_trade = await self.circuit_breaker.can_trade(current_equity)
            
            logger.info(
                "circuit_breaker_check",
                can_trade=can_trade,
                state=self.circuit_breaker.state.value,
                current_equity=float(current_equity)
            )
            
            if not can_trade:
                logger.warning(
                    "trade_blocked_by_circuit_breaker",
                    reason="Circuit breaker is OPEN",
                    status=self.circuit_breaker.get_status()
                )
                return
            
            # Risk check: Position sizing
            position_value = signal['quantity'] * signal['target_price']
            max_position_size = current_equity * Decimal('0.10')  # 10% max
            
            if position_value > max_position_size:
                logger.warning(
                    "position_size_exceeded",
                    position_value=float(position_value),
                    max_allowed=float(max_position_size),
                    action="skipping trade"
                )
                return
            
            logger.info(
                "risk_check_passed",
                position_value=float(position_value),
                max_allowed=float(max_position_size)
            )
            
            # STEP 4: Order Execution
            logger.info("\n--- ORDER EXECUTION ---")
            
            result = await self.execution.place_order(
                strategy="latency_arb",
                market_id=signal['market_id'],
                token_id=signal['token_id'],
                side=signal['side'],
                quantity=signal['quantity'],
                price=signal['target_price'],
                metadata={
                    'trigger_price': float(price_data.price),
                    'confidence': signal['confidence'],
                    'tick': self.tick_count
                }
            )
            
            if result.success:
                logger.info(
                    "order_executed_successfully",
                    order_id=result.order_id,
                    status=result.status.value,
                    filled_quantity=float(result.filled_quantity),
                    average_price=float(result.average_fill_price) if result.average_fill_price else None,
                    fees=float(result.fees)
                )
            else:
                logger.error(
                    "order_execution_failed",
                    error=result.error,
                    message=result.message
                )
                return
            
            # STEP 5: Ledger Entry (already done by ExecutionService)
            logger.info("\n--- LEDGER UPDATE ---")
            
            # Query updated state
            updated_equity = await self.ledger.get_equity()
            positions = await self.ledger.get_open_positions()
            
            logger.info(
                "ledger_updated",
                equity=float(updated_equity),
                open_positions=len(positions),
                pnl=float(updated_equity - current_equity)
            )
            
            # Show position details
            if positions:
                latest_position = positions[-1]
                logger.info(
                    "position_opened",
                    position_id=latest_position['id'],
                    market=latest_position['market_id'],
                    quantity=float(latest_position['quantity']),
                    entry_price=float(latest_position['entry_price'])
                )
            
            # STEP 6: Update Circuit Breaker
            # Simulate P&L after some time
            simulated_pnl = Decimal('5.50')  # Positive outcome
            await self.circuit_breaker.record_trade_result(
                updated_equity,
                simulated_pnl
            )
            
            logger.info(
                "circuit_breaker_updated",
                pnl=float(simulated_pnl),
                status=self.circuit_breaker.get_status()
            )
            
            logger.info("=" * 60)
            logger.info(f"TICK #{self.tick_count} COMPLETE\n")
            
        except Exception as e:
            logger.error(
                "tick_processing_failed",
                tick=self.tick_count,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
    
    async def run(self):
        """Run dry run test."""
        logger.info("\n" + "=" * 60)
        logger.info("STARTING DRY RUN")
        logger.info("=" * 60)
        logger.info(f"Processing {self.max_ticks} Binance ticks...\n")
        
        try:
            # Wait for ticks or timeout
            await asyncio.wait_for(
                self.test_complete.wait(),
                timeout=60.0
            )
        except asyncio.TimeoutError:
            logger.warning("dry_run_timeout", message="No price updates received in 60s")
        
        # Final summary
        await self._print_summary()
    
    async def _print_summary(self):
        """Print execution summary."""
        logger.info("\n" + "=" * 60)
        logger.info("DRY RUN SUMMARY")
        logger.info("=" * 60)
        
        # Get final state
        equity = await self.ledger.get_equity()
        positions = await self.ledger.get_open_positions()
        
        # Get all orders
        orders = list(self.execution.orders.values())
        
        logger.info(
            "final_state",
            ticks_processed=self.tick_count,
            orders_placed=len(orders),
            open_positions=len(positions),
            final_equity=float(equity),
            profit_loss=float(equity - Decimal('10000'))
        )
        
        # Circuit breaker status
        cb_status = self.circuit_breaker.get_status()
        logger.info(
            "circuit_breaker_final_status",
            state=cb_status['state'],
            trades=cb_status['total_trades'],
            wins=cb_status['winning_trades'],
            losses=cb_status['losing_trades']
        )
        
        logger.info("=" * 60)
        logger.info("DRY RUN COMPLETE")
        logger.info("=" * 60)
    
    async def cleanup(self):
        """Cleanup components."""
        logger.info("\nCleaning up...")
        
        if self.execution:
            await self.execution.stop()
        
        if self.websocket:
            await self.websocket.stop()
        
        if self.api_client:
            await self.api_client.close()
        
        if self.ledger:
            await self.ledger.close()
        
        logger.info("Cleanup complete")


async def main():
    """
    Run dry run.
    """
    dry_run = DryRun()
    
    try:
        await dry_run.initialize()
        await dry_run.run()
    
    except KeyboardInterrupt:
        logger.info("Dry run interrupted by user")
    
    except Exception as e:
        logger.error(
            "dry_run_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )
    
    finally:
        await dry_run.cleanup()


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("POLYMARKET DRY RUN")
    print("=" * 60)
    print("\nThis demonstrates the full execution flow:")
    print("  1. Binance WebSocket Tick")
    print("  2. Signal Generation")
    print("  3. Risk Check (Circuit Breaker)")
    print("  4. Order Execution")
    print("  5. Ledger Update")
    print("\nStarting in 3 seconds...\n")
    
    asyncio.run(asyncio.sleep(2))
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nDry run terminated by user.")
    
    print("\n" + "=" * 60)
    print("DRY RUN COMPLETE")
    print("="*60 + "\n")
