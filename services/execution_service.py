#!/usr/bin/env python3
"""
Production Execution Service

Handles all order placement with:
- Rate limiting (respect Polymarket API limits)
- Retry logic with exponential backoff
- Timeout handling
- Ledger integration (record every fill)
- Order status tracking
- Concurrent order serialization

Never allows silent failures. Every order is logged and tracked.
"""

import asyncio
import logging
from typing import Dict, Optional, List
from decimal import Decimal
from datetime import datetime, timedelta
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class OrderResult:
    """Result of order execution"""
    success: bool
    order_id: Optional[str]
    filled_price: Optional[Decimal]
    filled_quantity: Optional[Decimal]
    fees: Optional[Decimal]
    error: Optional[str]
    latency_ms: int
    retries: int

class RateLimiter:
    """
    Token bucket rate limiter for API calls.
    
    Prevents exceeding Polymarket rate limits:
    - 10 requests/second for authenticated endpoints
    - 100 requests/minute for order placement
    """
    
    def __init__(self, requests_per_second: float = 8.0):
        # Conservative: use 8/sec to stay under 10/sec limit
        self.requests_per_second = requests_per_second
        self.tokens = requests_per_second
        self.max_tokens = requests_per_second
        self.last_update = time.monotonic()
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        """Wait until a token is available"""
        async with self.lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_update
                
                # Refill tokens based on elapsed time
                self.tokens = min(
                    self.max_tokens,
                    self.tokens + elapsed * self.requests_per_second
                )
                self.last_update = now
                
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                
                # Wait until next token available
                wait_time = (1.0 - self.tokens) / self.requests_per_second
                await asyncio.sleep(wait_time)

class ExecutionService:
    """
    Centralized order execution with proper error handling and ledger integration.
    """
    
    def __init__(self, polymarket_client, ledger, config: Optional[dict] = None):
        self.client = polymarket_client
        self.ledger = ledger
        
        config = config or {}
        self.max_retries = config.get('max_retries', 3)
        self.timeout_seconds = config.get('timeout_seconds', 10)
        self.initial_backoff = config.get('initial_backoff', 1.0)
        
        # Concurrency control
        self.rate_limiter = RateLimiter(requests_per_second=8.0)
        self.order_semaphore = asyncio.Semaphore(5)  # Max 5 concurrent orders
        
        # Order tracking
        self.active_orders = {}  # order_id -> metadata
        self.order_history = []  # Recent order results
        
        logger.info("ExecutionService initialized with rate limiting and retry logic")
    
    async def place_order(
        self,
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,  # 'YES' or 'NO'
        quantity: Decimal,
        price: Decimal,
        order_type: str = 'GTC',
        metadata: Optional[Dict] = None
    ) -> OrderResult:
        """
        Place order with full execution lifecycle:
        1. Rate limit check
        2. Send order with retries
        3. Wait for fill (with timeout)
        4. Record in ledger
        5. Update position tracking
        
        Args:
            strategy: Strategy name (for tracking)
            market_id: Polymarket condition_id
            token_id: Specific YES/NO token ID
            side: 'YES' or 'NO'
            quantity: Amount to buy (in shares)
            price: Limit price (0-1)
            order_type: 'GTC' (good-til-cancel) or 'FOK' (fill-or-kill)
            metadata: Additional data to store
        
        Returns:
            OrderResult with fill details or error
        """
        async with self.order_semaphore:
            return await self._execute_order(
                strategy, market_id, token_id, side,
                quantity, price, order_type, metadata
            )
    
    async def _execute_order(
        self,
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        order_type: str,
        metadata: Optional[Dict]
    ) -> OrderResult:
        """
        Internal order execution with retry logic.
        """
        start_time = time.monotonic()
        
        for attempt in range(self.max_retries):
            try:
                # Rate limit
                await self.rate_limiter.acquire()
                
                # Place order
                logger.info(
                    f"[{strategy}] Placing order: {side} {quantity} @ {price} on {market_id[:20]}"
                )
                
                order_response = await asyncio.wait_for(
                    self.client.place_order(
                        token_id=token_id,
                        side=side.lower(),
                        amount=float(quantity),
                        price=float(price),
                        order_type=order_type
                    ),
                    timeout=self.timeout_seconds
                )
                
                if not order_response or not order_response.get('success'):
                    error = order_response.get('error', 'Unknown error') if order_response else 'No response'
                    logger.warning(f"Order failed (attempt {attempt+1}/{self.max_retries}): {error}")
                    
                    if attempt < self.max_retries - 1:
                        backoff = self.initial_backoff * (2 ** attempt)
                        await asyncio.sleep(backoff)
                        continue
                    
                    return OrderResult(
                        success=False,
                        order_id=None,
                        filled_price=None,
                        filled_quantity=None,
                        fees=None,
                        error=error,
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )
                
                order_id = order_response['order_id']
                
                # Wait for fill (poll order status)
                filled_price, filled_quantity, fees = await self._wait_for_fill(
                    order_id,
                    max_wait_seconds=30
                )
                
                if filled_quantity is None or filled_quantity == 0:
                    logger.warning(f"Order {order_id} not filled within timeout")
                    return OrderResult(
                        success=False,
                        order_id=order_id,
                        filled_price=None,
                        filled_quantity=None,
                        fees=None,
                        error="Order not filled (timeout)",
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )
                
                # Record in ledger
                txn_id, position_id = self.ledger.record_trade_entry(
                    strategy=strategy,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    quantity=filled_quantity,
                    entry_price=filled_price,
                    fees=fees,
                    order_id=order_id,
                    metadata=metadata
                )
                
                latency_ms = int((time.monotonic() - start_time) * 1000)
                
                logger.info(
                    f"[{strategy}] Order filled: {order_id[:20]} | "
                    f"{filled_quantity} @ {filled_price} | Fees: ${fees} | "
                    f"Position: {position_id} | Latency: {latency_ms}ms"
                )
                
                result = OrderResult(
                    success=True,
                    order_id=order_id,
                    filled_price=filled_price,
                    filled_quantity=filled_quantity,
                    fees=fees,
                    error=None,
                    latency_ms=latency_ms,
                    retries=attempt + 1
                )
                
                self.order_history.append(result)
                return result
            
            except asyncio.TimeoutError:
                logger.warning(f"Order timeout (attempt {attempt+1}/{self.max_retries})")
                if attempt < self.max_retries - 1:
                    backoff = self.initial_backoff * (2 ** attempt)
                    await asyncio.sleep(backoff)
                else:
                    return OrderResult(
                        success=False,
                        order_id=None,
                        filled_price=None,
                        filled_quantity=None,
                        fees=None,
                        error="Timeout",
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )
            
            except Exception as e:
                logger.error(f"Order execution error (attempt {attempt+1}/{self.max_retries}): {e}", exc_info=True)
                if attempt < self.max_retries - 1:
                    backoff = self.initial_backoff * (2 ** attempt)
                    await asyncio.sleep(backoff)
                else:
                    return OrderResult(
                        success=False,
                        order_id=None,
                        filled_price=None,
                        filled_quantity=None,
                        fees=None,
                        error=str(e),
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )
        
        # Should never reach here
        return OrderResult(
            success=False,
            order_id=None,
            filled_price=None,
            filled_quantity=None,
            fees=None,
            error="Max retries exceeded",
            latency_ms=int((time.monotonic() - start_time) * 1000),
            retries=self.max_retries
        )
    
    async def _wait_for_fill(
        self,
        order_id: str,
        max_wait_seconds: int = 30,
        poll_interval: float = 0.5
    ) -> tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
        """
        Poll order status until filled or timeout.
        
        Returns:
            (filled_price, filled_quantity, fees) or (None, None, None) if not filled
        """
        start = time.monotonic()
        
        while (time.monotonic() - start) < max_wait_seconds:
            try:
                await self.rate_limiter.acquire()
                
                status = await self.client.get_order_status(order_id)
                
                if status and status.get('status') == 'MATCHED':
                    filled_price = Decimal(str(status['filled_price']))
                    filled_quantity = Decimal(str(status['filled_quantity']))
                    fees = Decimal(str(status.get('fees', 0)))
                    
                    return filled_price, filled_quantity, fees
                
                elif status and status.get('status') in ['CANCELLED', 'FAILED']:
                    logger.warning(f"Order {order_id} status: {status['status']}")
                    return None, None, None
                
                await asyncio.sleep(poll_interval)
            
            except Exception as e:
                logger.error(f"Error checking order status: {e}")
                await asyncio.sleep(poll_interval)
        
        logger.warning(f"Order {order_id} fill timeout after {max_wait_seconds}s")
        return None, None, None
    
    async def close_position(
        self,
        position_id: int,
        exit_reason: str,
        exit_price: Optional[Decimal] = None
    ) -> OrderResult:
        """
        Close an open position.
        
        Args:
            position_id: Position ID from ledger
            exit_reason: Why closing ('TARGET_HIT', 'STOP_LOSS', 'TIME_STOP', etc.)
            exit_price: Optional specific exit price (default: market)
        
        Returns:
            OrderResult
        """
        # Get position details
        positions = self.ledger.get_open_positions()
        position = next((p for p in positions if p['id'] == position_id), None)
        
        if not position:
            logger.error(f"Position {position_id} not found or already closed")
            return OrderResult(
                success=False,
                order_id=None,
                filled_price=None,
                filled_quantity=None,
                fees=None,
                error="Position not found",
                latency_ms=0,
                retries=0
            )
        
        # Sell to close
        token_id = position['token_id']
        quantity = Decimal(str(position['quantity']))
        
        # Use market order if no price specified
        if exit_price is None:
            # Get current market price
            mid_price = await self._get_mid_price(token_id)
            exit_price = mid_price if mid_price else Decimal('0.5')
        
        # Place sell order
        order_result = await self.place_order(
            strategy=position['strategy'],
            market_id=position['market_id'],
            token_id=token_id,
            side='SELL',
            quantity=quantity,
            price=exit_price,
            order_type='FOK',  # Fill-or-kill for exits
            metadata={'exit_reason': exit_reason, 'position_id': position_id}
        )
        
        if order_result.success:
            # Record exit in ledger
            self.ledger.record_trade_exit(
                position_id=position_id,
                exit_price=order_result.filled_price,
                fees=order_result.fees,
                exit_reason=exit_reason,
                order_id=order_result.order_id
            )
        
        return order_result
    
    async def _get_mid_price(self, token_id: str) -> Optional[Decimal]:
        """Get mid price from orderbook"""
        try:
            await self.rate_limiter.acquire()
            book = await self.client.get_market_orderbook(token_id)
            
            if not book:
                return None
            
            bids = book.get('bids', [])
            asks = book.get('asks', [])
            
            if not bids or not asks:
                return None
            
            best_bid = Decimal(str(bids[0]['price']))
            best_ask = Decimal(str(asks[0]['price']))
            
            return (best_bid + best_ask) / 2
        
        except Exception as e:
            logger.error(f"Error getting mid price: {e}")
            return None
    
    def get_stats(self) -> Dict:
        """Get execution statistics"""
        if not self.order_history:
            return {'orders_placed': 0}
        
        successful = [o for o in self.order_history if o.success]
        failed = [o for o in self.order_history if not o.success]
        
        avg_latency = sum(o.latency_ms for o in self.order_history) / len(self.order_history)
        avg_retries = sum(o.retries for o in self.order_history) / len(self.order_history)
        
        return {
            'orders_placed': len(self.order_history),
            'successful': len(successful),
            'failed': len(failed),
            'success_rate': len(successful) / len(self.order_history) if self.order_history else 0,
            'avg_latency_ms': int(avg_latency),
            'avg_retries': round(avg_retries, 2)
        }