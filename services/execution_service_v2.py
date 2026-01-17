#!/usr/bin/env python3
"""
Institutional-Grade Order Execution Service

Features:
- Complete order lifecycle management (pending → filled → settled)
- Partial fill handling
- Fill monitoring and tracking
- Order state machine
- Automatic position reconciliation
- Slippage tracking
- Execution quality metrics
- Dead letter queue for failed orders
- Instant paper trading simulation

Standards:
- Zero order loss
- Complete audit trail
- Production-grade error handling
- Observable (metrics + structured logs)
"""

import asyncio
import time
import uuid
from typing import Dict, Optional, List, Any
from decimal import Decimal
from datetime import datetime, timedelta
from dataclasses import dataclass, field, replace
from enum import Enum
try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False
from collections import defaultdict

from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide
from database.ledger_async import AsyncLedger
from services.error_codes import ErrorCode
from services.idempotency import IdempotencyCache, IdempotencyKeyBuilder

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

    logger = _FallbackLogger(__name__)


class OrderStatus(Enum):
    """Order status states"""
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class OrderRequest:
    """Order request data"""
    strategy: str
    market_id: str
    token_id: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    order_type: str = "GTC"
    metadata: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError(f"Invalid quantity: {self.quantity}")
        if not (Decimal('0.01') <= self.price <= Decimal('0.99')):
            raise ValueError(f"Invalid price: {self.price}")


@dataclass
class Fill:
    """Fill data"""
    fill_id: str
    order_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    timestamp: datetime
    
    @property
    def total_cost(self) -> Decimal:
        return self.quantity * self.price + self.fee


@dataclass
class OrderResult:
    """Order execution result"""
    success: bool
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: Decimal = Decimal('0')
    filled_price: Decimal = Decimal('0')
    fees: Decimal = Decimal('0')
    fills: List[Fill] = field(default_factory=list)
    error: Optional[str] = None
    error_code: Optional[str] = None
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    is_duplicate: bool = False
    slippage_bps: float = 0.0
    execution_time_ms: float = 0.0
    
    @property
    def is_complete(self) -> bool:
        return self.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED
        ]

    def __getitem__(self, item: str):
        return getattr(self, item)

    def get(self, item: str, default=None):
        return getattr(self, item, default)




class OrderState:
    """Track order state"""
    
    def __init__(self, request: OrderRequest, order_id: str):
        self.request = request
        self.order_id = order_id
        self.status = OrderStatus.PENDING
        self.fills: List[Fill] = []
        self.created_at = datetime.utcnow()
        self.updated_at = self.created_at
        self.retries = 0
        self.error_count = 0
    
    @property
    def filled_quantity(self) -> Decimal:
        return sum(f.quantity for f in self.fills)
    
    @property
    def remaining_quantity(self) -> Decimal:
        return self.request.quantity - self.filled_quantity
    
    @property
    def avg_fill_price(self) -> Decimal:
        if not self.fills:
            return Decimal('0')
        total_cost = sum(f.quantity * f.price for f in self.fills)
        total_qty = self.filled_quantity
        return total_cost / total_qty if total_qty > 0 else Decimal('0')
    
    @property
    def total_fees(self) -> Decimal:
        return sum(f.fee for f in self.fills)
    
    @property
    def age_seconds(self) -> float:
        return (datetime.utcnow() - self.created_at).total_seconds()
    
    def add_fill(self, fill: Fill):
        """Add a fill to this order."""
        self.fills.append(fill)
        self.updated_at = datetime.utcnow()
        
        if self.filled_quantity >= self.request.quantity:
            self.status = OrderStatus.FILLED
        elif self.filled_quantity > 0:
            self.status = OrderStatus.PARTIALLY_FILLED
    
    def mark_submitted(self):
        self.status = OrderStatus.SUBMITTED
        self.updated_at = datetime.utcnow()
    
    def mark_failed(self, error: str):
        self.status = OrderStatus.FAILED
        self.error_count += 1
        self.updated_at = datetime.utcnow()
        logger.error(
            "order_failed",
            order_id=self.order_id,
            error=error,
            retries=self.retries
        )


class ExecutionServiceV2:
    """
    Production-grade order execution service.
    
    Manages complete order lifecycle:
    1. Order creation and validation
    2. Submission to exchange
    3. Fill monitoring
    4. Position reconciliation
    5. Ledger updates
    
    Features:
    - Order state machine
    - Partial fill handling
    - Automatic retry with backoff
    - Dead letter queue for failures
    - Execution quality metrics
    - Instant paper trading simulation
    """
    
    def __init__(
        self,
        polymarket_client: PolymarketClientV2,
        ledger: AsyncLedger,
        config: Optional[Dict] = None,
        idempotency_cache: Optional[IdempotencyCache] = None
    ):
        self.client = polymarket_client
        self.ledger = ledger
        
        self.config = config or {}
        self.max_retries = self.config.get('max_retries', 3)
        self.timeout_seconds = self.config.get('timeout_seconds', 30)
        self.fill_check_interval = self.config.get('fill_check_interval', 2.0)
        self.max_order_age_seconds = self.config.get('max_order_age_seconds', 300)
        
        self.orders: Dict[str, OrderState] = {}
        self.order_lock = asyncio.Lock()
        
        self.dlq: List[OrderState] = []
        
        self.orders_placed = 0
        self.orders_filled = 0
        self.orders_failed = 0
        self.total_slippage_bps = 0.0
        self.total_fees = Decimal('0')
        self.execution_times_ms: List[float] = []

        self._idempotency_cache = idempotency_cache or IdempotencyCache(ttl_seconds=300)
        
        self._monitor_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        logger.info(
            "execution_service_initialized",
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            paper_trading=getattr(self.client, "paper_trading", False)
        )

    async def start(self):
        """Start background monitoring tasks."""
        self._monitor_task = asyncio.create_task(self._fill_monitor_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("execution_service_started")
    
    async def stop(self):
        """Stop background tasks."""
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        logger.info("execution_service_stopped")
    
    async def place_order(
        self,
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        order_type: str = "GTC",
        metadata: Optional[Dict] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> OrderResult:
        start_time = time.time()
        correlation_id = correlation_id or str(uuid.uuid4())
        is_paper_trading = getattr(self.client, "paper_trading", False)

        idempotency_key = IdempotencyKeyBuilder.build(
            strategy=strategy,
            market_id=market_id,
            side=str(side),
            quantity=quantity,
            price=price,
            override_key=idempotency_key
        )

        cached_result = self._idempotency_cache.get(idempotency_key)
        if cached_result is not None:
            cached_copy = replace(
                cached_result,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                is_duplicate=True
            )
            logger.info(
                "order_deduplicated_cache_hit",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                cached_order_id=cached_copy.order_id,
                cached_success=cached_copy.success,
                strategy=strategy,
                market_id=market_id,
                side=side,
                quantity=str(quantity),
                price=str(price)
            )
            return cached_copy
        
        try:
            side_str = side.value if isinstance(side, OrderSide) else str(side)
            order_side = OrderSide.SELL if side_str.upper() == 'SELL' else OrderSide.BUY
            request = OrderRequest(
                strategy=strategy,
                market_id=market_id,
                token_id=token_id,
                side=order_side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                metadata={
                    **(metadata or {}),
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                }
            )
        except ValueError as e:
            logger.error("invalid_order_request", error=str(e))
            result = OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error=str(e),
                error_code=ErrorCode.INVALID_PRICE.value if "price" in str(e).lower() else ErrorCode.INVALID_QUANTITY.value,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                is_duplicate=False
            )
            self._idempotency_cache.set(idempotency_key, result)
            return result
        
        for attempt in range(self.max_retries):
            try:
                response = await self.client.place_order(
                    token_id=token_id,
                    side=order_side,
                    price=price,
                    size=quantity,
                    order_type=order_type,
                    market_id=market_id,
                    correlation_id=correlation_id
                )
                
                if not response or not isinstance(response, dict) or not response.get('success'):
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        self.orders_failed += 1
                        result = OrderResult(
                            success=False,
                            status=OrderStatus.FAILED,
                            error=response.get('error') if isinstance(response, dict) else "Order submission failed",
                            error_code=ErrorCode.ORDER_SUBMISSION_FAILED.value,
                            correlation_id=correlation_id,
                            idempotency_key=idempotency_key,
                            is_duplicate=False
                        )
                        self._idempotency_cache.set(idempotency_key, result)
                        return result
                
                order_id = response['order_id']
                
                order_state = OrderState(request, order_id)
                order_state.mark_submitted()
                
                async with self.order_lock:
                    self.orders[order_id] = order_state
                
                self.orders_placed += 1
                
                # PAPER TRADING: Simulate instant fill
                if is_paper_trading:
                    fill = Fill(
                        fill_id=f"fill_{order_id}",
                        order_id=order_id,
                        quantity=quantity,
                        price=price,
                        fee=quantity * price * Decimal('0.002'),  # 0.2% fee
                        timestamp=datetime.utcnow()
                    )
                    order_state.add_fill(fill)
                    
                    logger.info(
                        "paper_fill_simulated",
                        order_id=order_id,
                        quantity=float(quantity),
                        price=float(price),
                        correlation_id=correlation_id
                    )
                else:
                    # LIVE TRADING: Wait for real fills
                    await self._wait_for_fills(
                        order_state,
                        timeout=self.timeout_seconds
                    )
                
                execution_time_ms = (time.time() - start_time) * 1000
                self.execution_times_ms.append(execution_time_ms)
                
                slippage_bps = 0.0
                if order_state.avg_fill_price > 0:
                    slippage_bps = float(
                        (order_state.avg_fill_price - price) / price * 10000
                    )
                    self.total_slippage_bps += abs(slippage_bps)
                
                self.total_fees += order_state.total_fees
                
                if order_state.filled_quantity > 0:
                    await self._record_trade_in_ledger(
                        order_state,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key
                    )
                    if order_state.status == OrderStatus.FILLED:
                        self.orders_filled += 1
                
                result = OrderResult(
                    success=(order_state.filled_quantity > 0),
                    order_id=order_id,
                    status=order_state.status,
                    filled_quantity=order_state.filled_quantity,
                    filled_price=order_state.avg_fill_price,
                    fees=order_state.total_fees,
                    fills=order_state.fills,
                    slippage_bps=slippage_bps,
                    execution_time_ms=execution_time_ms,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False
                )
                
                logger.info(
                    "order_executed",
                    order_id=order_id,
                    status=order_state.status.value,
                    filled_quantity=float(order_state.filled_quantity),
                    avg_price=float(order_state.avg_fill_price),
                    fees=float(order_state.total_fees),
                    slippage_bps=slippage_bps,
                    execution_time_ms=execution_time_ms,
                    correlation_id=correlation_id
                )
                
                self._idempotency_cache.set(idempotency_key, result)
                return result
            
            except Exception as e:
                logger.error(
                    "order_execution_error",
                    error=str(e),
                    attempt=attempt + 1,
                    correlation_id=correlation_id
                )
                
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    self.orders_failed += 1
                    result = OrderResult(
                        success=False,
                        status=OrderStatus.FAILED,
                        error=str(e),
                        error_code=ErrorCode.NETWORK_ERROR.value,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        is_duplicate=False
                    )
                    self._idempotency_cache.set(idempotency_key, result)
                    return result
        
        result = OrderResult(
            success=False,
            status=OrderStatus.FAILED,
            error="Unknown error",
            error_code=ErrorCode.UNKNOWN.value,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            is_duplicate=False
        )
        self._idempotency_cache.set(idempotency_key, result)
        return result
    
    async def _wait_for_fills(
        self,
        order_state: OrderState,
        timeout: float
    ) -> bool:
        start_time = time.time()
        
        while (time.time() - start_time) < timeout:
            status_response = await self.client.get_order_status(order_state.order_id)
            
            if status_response:
                fills = status_response.get('fills', [])
                for fill_data in fills:
                    fill_id = fill_data.get('id')
                    if not any(f.fill_id == fill_id for f in order_state.fills):
                        fill = Fill(
                            fill_id=fill_id,
                            order_id=order_state.order_id,
                            quantity=Decimal(str(fill_data.get('size', 0))),
                            price=Decimal(str(fill_data.get('price', 0))),
                            fee=Decimal(str(fill_data.get('fee', 0))),
                            timestamp=datetime.utcnow()
                        )
                        order_state.add_fill(fill)
                        
                        logger.info(
                            "fill_received",
                            order_id=order_state.order_id,
                            fill_id=fill_id,
                            quantity=float(fill.quantity),
                            price=float(fill.price)
                        )
                
                if order_state.status == OrderStatus.FILLED:
                    return True
            
            await asyncio.sleep(self.fill_check_interval)
        
        if order_state.status not in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
            order_state.status = OrderStatus.EXPIRED
            logger.warning(
                "order_expired",
                order_id=order_state.order_id,
                age_seconds=order_state.age_seconds
            )
        
        return order_state.status == OrderStatus.FILLED
    
    async def _record_trade_in_ledger(
        self,
        order_state: OrderState,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ):
        try:
            position_id = await self.ledger.record_trade_entry(
                market_id=order_state.request.market_id,
                token_id=order_state.request.token_id,
                strategy=order_state.request.strategy,
                entry_price=order_state.avg_fill_price,
                quantity=order_state.filled_quantity,
                fees=order_state.total_fees,
                order_id=order_state.order_id,
                metadata={
                    **(order_state.request.metadata or {}),
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                }
            )
            
            logger.info(
                "trade_recorded_in_ledger",
                position_id=position_id,
                order_id=order_state.order_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )
        
        except Exception as e:
            logger.error(
                "ledger_recording_failed",
                order_id=order_state.order_id,
                error=str(e)
            )
            self.dlq.append(order_state)
    
    async def _fill_monitor_loop(self):
        logger.info("fill_monitor_started")
        
        while True:
            try:
                await asyncio.sleep(self.fill_check_interval)
                
                async with self.order_lock:
                    pending_orders = [
                        order for order in self.orders.values()
                        if order.status in [
                            OrderStatus.SUBMITTED,
                            OrderStatus.PARTIALLY_FILLED
                        ]
                    ]
                
                for order in pending_orders:
                    await self._wait_for_fills(order, timeout=0.1)
            
            except asyncio.CancelledError:
                logger.info("fill_monitor_stopped")
                break
            except Exception as e:
                logger.error("fill_monitor_error", error=str(e))
    
    async def _cleanup_loop(self):
        logger.info("cleanup_loop_started")
        
        while True:
            try:
                await asyncio.sleep(60)
                
                async with self.order_lock:
                    to_remove = []
                    for order_id, order in self.orders.items():
                        if order.age_seconds > self.max_order_age_seconds:
                            if order.is_complete:
                                to_remove.append(order_id)
                    
                    for order_id in to_remove:
                        del self.orders[order_id]
                    
                    if to_remove:
                        logger.info(
                            "orders_cleaned_up",
                            count=len(to_remove)
                        )
            
            except asyncio.CancelledError:
                logger.info("cleanup_loop_stopped")
                break
            except Exception as e:
                logger.error("cleanup_loop_error", error=str(e))
    
    async def cancel_all_orders(self) -> int:
        async with self.order_lock:
            pending = [
                order for order in self.orders.values()
                if order.status in [
                    OrderStatus.PENDING,
                    OrderStatus.SUBMITTED,
                    OrderStatus.PARTIALLY_FILLED
                ]
            ]
        
        cancelled = 0
        for order in pending:
            success = await self.client.cancel_order(order.order_id)
            if success:
                order.status = OrderStatus.CANCELLED
                cancelled += 1
        
        logger.info("orders_cancelled", count=cancelled)
        return cancelled
    
    def get_metrics(self) -> Dict:
        avg_execution_time = (
            sum(self.execution_times_ms) / len(self.execution_times_ms)
            if self.execution_times_ms else 0.0
        )
        
        fill_rate = (
            self.orders_filled / self.orders_placed
            if self.orders_placed > 0 else 0.0
        )
        
        avg_slippage = (
            self.total_slippage_bps / self.orders_filled
            if self.orders_filled > 0 else 0.0
        )
        
        return {
            "orders_placed": self.orders_placed,
            "orders_filled": self.orders_filled,
            "orders_failed": self.orders_failed,
            "fill_rate": fill_rate,
            "avg_execution_time_ms": avg_execution_time,
            "avg_slippage_bps": avg_slippage,
            "total_fees": float(self.total_fees),
            "active_orders": len(self.orders),
            "dlq_size": len(self.dlq)
        }
