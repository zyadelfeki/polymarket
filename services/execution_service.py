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
import json
import os
import tempfile
from pathlib import Path
from services.network_health import NetworkHealthMonitor, NetworkPartitionError
from exports.positions_publisher import build_positions_from_ledger, PolymarketPositionsPublisher
from shared.risk_aggregator import Position, UnifiedRiskAggregator

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
    error_code: Optional[str] = None


class SlippageError(RuntimeError):
    """Raised when fill price exceeds slippage tolerance."""

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

        partition_threshold = int(config.get('partition_threshold_seconds', 15))
        shared_network_monitor = getattr(self.client, 'network_monitor', None)
        if isinstance(shared_network_monitor, NetworkHealthMonitor):
            shared_network_monitor.state.partition_threshold_seconds = partition_threshold
            self.network_monitor = shared_network_monitor
        else:
            self.network_monitor = NetworkHealthMonitor(partition_threshold_seconds=partition_threshold)
        
        # Order tracking
        self.active_orders = {}  # order_id -> metadata
        self.order_history = []  # Recent order results

        self.max_btc_exposure_usd = Decimal(str(config.get('max_btc_exposure_usd', '1000')))
        self.positions_publisher = PolymarketPositionsPublisher(self.ledger)
        
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
        metadata: Optional[Dict] = None,
        expected_price: Optional[Decimal] = None,
        min_profit_buffer_pct: Decimal = Decimal("0.05"),
        fee_rate: Decimal = Decimal("0.02"),
        max_slippage_bps: int = 50
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
        if expected_price is not None:
            if not isinstance(expected_price, Decimal):
                raise TypeError("expected_price must be Decimal")
            breakeven = self.ledger.calculate_breakeven_price(price, quantity, fee_rate)
            min_target_price = breakeven * (Decimal("1") + min_profit_buffer_pct)
            if expected_price < min_target_price:
                logger.debug(
                    f"Skipping order: expected price {expected_price} below breakeven buffer {min_target_price}"
                )
                return OrderResult(
                    success=False,
                    order_id=None,
                    filled_price=None,
                    filled_quantity=None,
                    fees=None,
                    error="below_breakeven",
                    latency_ms=0,
                    retries=0
                )

        risk_blocked = self._check_unified_risk(side, quantity, price, metadata)
        if risk_blocked is not None:
            return risk_blocked
        async with self.order_semaphore:
            return await self._execute_order(
                strategy, market_id, token_id, side,
                quantity, price, order_type, metadata, max_slippage_bps
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
        metadata: Optional[Dict],
        max_slippage_bps: int
    ) -> OrderResult:
        """
        Internal order execution with retry logic.
        """
        start_time = time.monotonic()
        is_paper_mode = bool(getattr(self.client, "paper_trading", False))

        if (not is_paper_mode) and self.network_monitor.check_partition():
            return OrderResult(
                success=False,
                order_id=None,
                filled_price=None,
                filled_quantity=None,
                fees=None,
                error="network_partition",
                error_code="NETWORK_PARTITION",
                latency_ms=int((time.monotonic() - start_time) * 1000),
                retries=0
            )
        
        if is_paper_mode:
            try:
                order_id = f"paper_{int(time.time() * 1000)}"
                filled_price = Decimal(str(price))
                filled_quantity = Decimal(str(quantity))
                fees = Decimal("0")

                correlation_id = ""
                if metadata and isinstance(metadata, dict):
                    correlation_id = str(metadata.get("correlation_id", ""))

                position_id = await self.ledger.record_trade_entry(
                    order_id=order_id,
                    strategy=strategy,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    quantity=filled_quantity,
                    price=filled_price,
                    correlation_id=correlation_id,
                    metadata=metadata,
                )

                latency_ms = int((time.monotonic() - start_time) * 1000)
                logger.info(
                    f"[{strategy}] Paper order simulated: {order_id[:20]} | "
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
                    retries=1,
                )
                self.order_history.append(result)
                return result
            except Exception as e:
                logger.error(f"Paper order simulation error: {e}", exc_info=True)
                return OrderResult(
                    success=False,
                    order_id=None,
                    filled_price=None,
                    filled_quantity=None,
                    fees=None,
                    error=str(e),
                    error_code="PAPER_SIM_ERROR",
                    latency_ms=int((time.monotonic() - start_time) * 1000),
                    retries=1,
                )

        for attempt in range(self.max_retries):
            try:
                # Rate limit
                await self.rate_limiter.acquire()
                
                # Place order
                logger.info(
                    f"[{strategy}] Placing order: {side} {quantity} @ {price} on {market_id[:20]}"
                )

                normalized_side = side.upper()
                if normalized_side in {"YES", "NO"}:
                    normalized_side = "BUY"
                
                order_response = await asyncio.wait_for(
                    self.client.place_order(
                        market_id=market_id,
                        token_id=token_id,
                        side=normalized_side,
                        size=quantity,
                        price=price,
                        order_type=order_type
                    ),
                    timeout=self.timeout_seconds
                )
                self.network_monitor.record_success()
                
                if order_response is None or not order_response.get('success'):
                    error = order_response.get('error', 'Unknown error') if order_response is not None else 'No response'
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
                
                order_id = order_response.get('order_id') or order_response.get('orderID')
                if not order_id:
                    logger.warning(f"Order response missing order_id (attempt {attempt+1}/{self.max_retries})")
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
                        error="missing_order_id",
                        error_code="INVALID_ORDER_RESPONSE",
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1,
                    )

                if getattr(self.client, "paper_trading", False):
                    filled_price = Decimal(str(order_response.get('avg_price') or price))
                    filled_quantity = Decimal(str(order_response.get('filled_size') or quantity))
                    fees = Decimal("0")
                else:
                    # Wait for fill (poll order status)
                    filled_price, filled_quantity, fees = await self._wait_for_fill(
                        order_id,
                        max_wait_seconds=30,
                        target_price=price,
                        max_slippage_bps=max_slippage_bps
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
                        error_code="ORDER_TIMEOUT",
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )

                # Publish positions after fill for unified risk
                try:
                    self.positions_publisher.publish_positions()
                except Exception as exc:
                    logger.warning(f"Failed to publish positions: {exc}")

                
                correlation_id = ""
                if metadata and isinstance(metadata, dict):
                    correlation_id = str(metadata.get("correlation_id", ""))

                # Record in ledger
                position_id = await self.ledger.record_trade_entry(
                    order_id=order_id,
                    strategy=strategy,
                    market_id=market_id,
                    token_id=token_id,
                    side=side,
                    quantity=filled_quantity,
                    price=filled_price,
                    correlation_id=correlation_id,
                    metadata=metadata,
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
            
            except SlippageError as e:
                logger.error(f"Slippage violation: {e}")
                return OrderResult(
                    success=False,
                    order_id=None,
                    filled_price=None,
                    filled_quantity=None,
                    fees=None,
                    error="slippage_violation",
                    error_code="SLIPPAGE_VIOLATION",
                    latency_ms=int((time.monotonic() - start_time) * 1000),
                    retries=attempt + 1
                )
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
                        error_code="ORDER_TIMEOUT",
                        latency_ms=int((time.monotonic() - start_time) * 1000),
                        retries=attempt + 1
                    )
            
            except Exception as e:
                self.network_monitor.record_failure(str(e))
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
                        error_code="EXECUTION_ERROR",
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
            error_code="MAX_RETRIES",
            latency_ms=int((time.monotonic() - start_time) * 1000),
            retries=self.max_retries
        )
    
    def _check_unified_risk(
        self,
        side: str,
        quantity: Decimal,
        price: Decimal,
        metadata: Optional[Dict]
    ) -> Optional[OrderResult]:
        if not self._is_btc_market(metadata):
            return None

        direction = self._infer_position_direction(side, metadata)
        if direction is None:
            return None

        size_usd = quantity * price

        aggregator = UnifiedRiskAggregator(max_btc_exposure_usd=self.max_btc_exposure_usd)
        positions = self._load_unified_positions()
        aggregator.update_positions(positions)

        can_open, reason = aggregator.can_open_btc_position(size_usd, direction)
        if not can_open:
            logger.warning(f"⚠️ Order blocked by unified risk: {reason}")
            return OrderResult(
                success=False,
                order_id=None,
                filled_price=None,
                filled_quantity=None,
                fees=None,
                error="risk_blocked",
                error_code="RISK_LIMIT",
                latency_ms=0,
                retries=0
            )

        return None

    def _load_unified_positions(self) -> List[Position]:
        positions: List[Position] = []

        # Local positions from ledger
        try:
            positions.extend(build_positions_from_ledger(self.ledger))
        except Exception as exc:
            logger.warning(f"Failed to load ledger positions: {exc}")

        # External crypto positions from shared file
        crypto_path = self._positions_file("CHARLIE_POSITIONS_FILE", "crypto_positions.json")
        try:
            if crypto_path.exists():
                raw = json.loads(crypto_path.read_text())
                for item in raw:
                    positions.append(
                        Position(
                            bot=item["bot"],
                            asset=item["asset"],
                            direction=item["direction"],
                            notional_value=Decimal(str(item["notional_value"])),
                            source=item.get("source", "bitget"),
                        )
                    )
        except Exception as exc:
            logger.warning(f"Failed to load crypto positions: {exc}")

        return positions

    def _is_btc_market(self, metadata: Optional[Dict]) -> bool:
        if not metadata:
            return False

        symbol = str(metadata.get("symbol", "")).upper()
        if symbol == "BTC":
            return True

        question = str(metadata.get("question") or metadata.get("market_question") or "").lower()
        return "btc" in question or "bitcoin" in question

    def _infer_position_direction(self, side: str, metadata: Optional[Dict]) -> Optional[str]:
        side = side.upper()
        if side not in {"YES", "NO"}:
            return None

        question = ""
        if metadata:
            question = str(metadata.get("question") or metadata.get("market_question") or "").lower()

        is_below = any(word in question for word in ["below", "under", "less than", "<"])

        if is_below:
            yes_direction = "SHORT"
            no_direction = "LONG"
        else:
            yes_direction = "LONG"
            no_direction = "SHORT"

        return yes_direction if side == "YES" else no_direction

    def _positions_file(self, env_key: str, filename: str) -> Path:
        env_path = os.getenv(env_key)
        if env_path:
            return Path(env_path)

        if os.name == "nt":
            return Path(tempfile.gettempdir()) / filename

        return Path(f"/tmp/{filename}")

    async def _wait_for_fill(
        self,
        order_id: str,
        max_wait_seconds: int = 30,
        poll_interval: float = 0.5,
        target_price: Optional[Decimal] = None,
        max_slippage_bps: int = 50
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
                if status:
                    self.network_monitor.record_success()
                
                if status and status.get('status') == 'MATCHED':
                    filled_price = Decimal(str(status['filled_price']))
                    filled_quantity = Decimal(str(status['filled_quantity']))
                    fees = Decimal(str(status.get('fees', 0)))

                    if target_price is not None:
                        max_slippage = Decimal(str(max_slippage_bps)) / Decimal("10000")
                        max_fill_price = target_price * (Decimal("1") + max_slippage)
                        min_fill_price = target_price * (Decimal("1") - max_slippage)
                        if filled_price > max_fill_price or filled_price < min_fill_price:
                            try:
                                if hasattr(self.client, "cancel_order"):
                                    await self.client.cancel_order(order_id)
                            finally:
                                raise SlippageError(
                                    f"Fill price {filled_price} outside tolerance for target {target_price}"
                                )

                    return filled_price, filled_quantity, fees
                
                elif status and status.get('status') in ['CANCELLED', 'FAILED']:
                    logger.warning(f"Order {order_id} status: {status['status']}")
                    return None, None, None
                
                await asyncio.sleep(poll_interval)

            except SlippageError:
                raise
            except Exception as e:
                self.network_monitor.record_failure(str(e))
                logger.error(f"Error checking order status: {e}")
                await asyncio.sleep(poll_interval)
        
        logger.warning(f"Order {order_id} fill timeout after {max_wait_seconds}s")
        if hasattr(self.client, "cancel_order"):
            try:
                await self.client.cancel_order(order_id)
            except Exception:
                pass
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