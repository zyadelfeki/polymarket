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
from typing import Dict, Optional, List, Any
from decimal import Decimal, getcontext
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from enum import Enum
try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False
from collections import defaultdict

getcontext().prec = 18

from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide
from database.ledger_async import AsyncLedger
from services.error_codes import (
    ErrorCode,
    TradingException,
    ValidationError,
    OperationalError,
    RETRYABLE_CODES,
)
from services.idempotency import IdempotencyCache, IdempotencyKeyBuilder
from execution.idempotency_manager import IdempotencyManager
from services.validators import BoundaryValidator
from services.correlation_context import CorrelationContext, inject_correlation
from logs.precision_monitor import PrecisionMonitor, PrecisionError
from services.retry import RetryableOperation
from utils.correlation_id import generate_correlation_id
from services.network_health import NetworkHealthMonitor, NetworkPartitionError
from utils.decimal_helpers import to_decimal, to_timeout_float

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
            kwargs = inject_correlation(kwargs)
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

        def critical(self, event: str, **kwargs):
            self._log(logging.CRITICAL, event, **kwargs)

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
        if not isinstance(self.quantity, Decimal):
            raise TypeError(f"Quantity must be Decimal: {type(self.quantity)}")
        if not isinstance(self.price, Decimal):
            raise TypeError(f"Price must be Decimal: {type(self.price)}")
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


class OrderResult(dict):
    """Order execution result (dict-like for compatibility)."""

    def __init__(
        self,
        *,
        success: bool,
        order_id: Optional[str] = None,
        status: OrderStatus = OrderStatus.PENDING,
        filled_quantity: Decimal = Decimal('0'),
        filled_price: Decimal = Decimal('0'),
        fees: Decimal = Decimal('0'),
        fills: Optional[List[Fill]] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        is_duplicate: bool = False,
        slippage_bps: Decimal = Decimal("0"),
        execution_time_ms: float = 0.0,
    ):
        super().__init__(
            success=success,
            order_id=order_id,
            status=status,
            filled_quantity=filled_quantity,
            filled_price=filled_price,
            fees=fees,
            fills=fills or [],
            error=error,
            error_code=error_code,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            is_duplicate=is_duplicate,
            slippage_bps=slippage_bps,
            execution_time_ms=execution_time_ms,
        )

    def __getattr__(self, item: str):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value) -> None:
        self[key] = value

    @property
    def is_complete(self) -> bool:
        return self.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED,
        ]




class OrderState:
    """Track order state"""
    
    def __init__(self, request: OrderRequest, order_id: str):
        self.request = request
        self.order_id = order_id
        self.status = OrderStatus.PENDING
        self.fills: List[Fill] = []
        self.created_at = datetime.now(timezone.utc)
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
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()
    
    def add_fill(self, fill: Fill):
        """Add a fill to this order."""
        self.fills.append(fill)
        self.updated_at = datetime.now(timezone.utc)
        
        if self.filled_quantity >= self.request.quantity:
            self.status = OrderStatus.FILLED
        elif self.filled_quantity > 0:
            self.status = OrderStatus.PARTIALLY_FILLED
    
    def mark_submitted(self):
        self.status = OrderStatus.SUBMITTED
        self.updated_at = datetime.now(timezone.utc)
    
    def mark_failed(self, error: str):
        self.status = OrderStatus.FAILED
        self.error_count += 1
        self.updated_at = datetime.now(timezone.utc)
        logger.error(
            "order_failed",
            order_id=self.order_id,
            error=error,
            retries=self.retries
        )

    @property
    def is_complete(self) -> bool:
        return self.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.EXPIRED,
        ]


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
        idempotency_cache: Optional[IdempotencyCache] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        risk_aggregator: Optional[Any] = None,
        do_not_trade_registry: Optional[Any] = None,
    ):
        self.client = polymarket_client
        self.ledger = ledger

        self.circuit_breaker_active = False
        
        self.config = config or {}
        self.max_retries = self.config.get('max_retries', 3)
        self.timeout_seconds = to_decimal(self.config.get('timeout_seconds', '30'))
        self.fill_check_interval = to_decimal(self.config.get('fill_check_interval', '2.0'))
        self.max_order_age_seconds = self.config.get('max_order_age_seconds', 300)

        # Auto-block threshold: if realised slippage on a successful fill exceeds
        # this many basis points, the market gets added to the do-not-trade list.
        # Default 200 bps (2%) — configurable via execution.auto_block_slippage_bps.
        self._auto_block_slippage_bps: Decimal = Decimal(
            str(self.config.get("auto_block_slippage_bps", 200))
        )
        # Registry for auto-blocking markets with excessive slippage.
        self._do_not_trade_registry = do_not_trade_registry
        
        self.orders: Dict[str, OrderState] = {}
        self.order_lock = asyncio.Lock()
        
        self.dlq: List[OrderState] = []
        
        self.orders_placed = 0
        self.orders_filled = 0
        self.orders_failed = 0
        self.total_slippage_bps = Decimal("0")
        self.total_fees = Decimal('0')
        self.execution_times_ms: List[float] = []

        self._idempotency_cache = idempotency_cache or IdempotencyCache()
        self._idempotency_manager = idempotency_manager
        self._risk_aggregator = risk_aggregator
        self.precision_monitor = PrecisionMonitor()

        partition_threshold = int(self.config.get("partition_threshold_seconds", 15))
        self.network_monitor = NetworkHealthMonitor(partition_threshold_seconds=partition_threshold)
        
        self._monitor_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        logger.info(
            "execution_service_initialized",
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            paper_trading=getattr(self.client, "paper_trading", False)
        )

    async def get_real_balance(self) -> Decimal:
        """Fetch actual wallet balance from Polymarket API."""
        try:
            balance_response = await self.client.get_wallet_balance()
            if not balance_response or "balance" not in balance_response:
                raise ValueError("Invalid balance response from API")

            balance = Decimal(str(balance_response["balance"]))

            if balance <= Decimal("0"):
                logger.critical("zero_balance_detected_halting_trading")
                self.circuit_breaker_active = True
                raise ValueError("Cannot trade with zero balance")

            logger.info("real_balance_fetched", balance=str(balance))
            return balance
        except Exception as exc:
            logger.critical("failed_to_fetch_balance", error=str(exc))
            self.circuit_breaker_active = True
            raise

    async def place_order_with_idempotency(
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
    ) -> OrderResult:
        """Place order with file-backed idempotency protection."""
        if not self._idempotency_manager:
            return await self.place_order(
                strategy=strategy,
                market_id=market_id,
                token_id=token_id,
                side=side,
                quantity=quantity,
                price=price,
                order_type=order_type,
                metadata=metadata,
                correlation_id=correlation_id,
            )

        idem_key = self._idempotency_manager.generate_key(
            market_id=market_id,
            side=side,
            size=quantity,
            price=price,
            strategy=strategy,
        )

        cached = self._idempotency_manager.check_duplicate(idem_key)
        if cached is not None:
            if isinstance(cached, OrderResult):
                return cached
            if isinstance(cached, dict):
                return OrderResult(**cached)
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="invalid_idempotency_cache",
                error_code=ErrorCode.INVALID_STATE.value,
                correlation_id=correlation_id,
                idempotency_key=idem_key,
                is_duplicate=True,
            )

        result = await self.place_order(
            strategy=strategy,
            market_id=market_id,
            token_id=token_id,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            metadata=metadata,
            correlation_id=correlation_id,
            idempotency_key=idem_key,
        )

        if result.success:
            self._idempotency_manager.record_order(idem_key, dict(result))

        return result

    async def place_order_with_risk_check(
        self,
        trade_delta: Decimal,
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        order_type: str = "GTC",
        metadata: Optional[Dict] = None,
        correlation_id: Optional[str] = None,
    ) -> OrderResult:
        """Place order only if within unified risk limits."""
        if self._risk_aggregator and not self._risk_aggregator.can_place_trade(trade_delta):
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="risk_limit_exceeded",
                error_code=ErrorCode.INSUFFICIENT_CAPITAL.value,
                correlation_id=correlation_id,
                is_duplicate=False,
            )

        return await self.place_order_with_idempotency(
            strategy=strategy,
            market_id=market_id,
            token_id=token_id,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            metadata=metadata,
            correlation_id=correlation_id,
        )

    async def start(self):
        """Start background monitoring tasks."""
        self._monitor_task = asyncio.create_task(self._fill_monitor_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("execution_service_started")

    @staticmethod
    def _resolve_error_code(value: Optional[str]) -> ErrorCode:
        if not value:
            return ErrorCode.UNKNOWN
        for code in ErrorCode:
            if code.value == value:
                return code
        return ErrorCode.UNKNOWN

    async def _record_order_audit(
        self,
        *,
        order_id: str,
        old_state: Optional[str],
        new_state: str,
        reason: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> None:
        if not hasattr(self.ledger, "record_audit_event"):
            return
        await self.ledger.record_audit_event(
            entity_type="order",
            entity_id=order_id,
            old_state=old_state,
            new_state=new_state,
            reason=reason,
            context={
                **(context or {}),
                "idempotency_key": idempotency_key,
            },
            correlation_id=correlation_id,
        )

    @staticmethod
    def calculate_profit_loss(
        quantity: Decimal,
        entry_price: Decimal,
        exit_price: Decimal,
        fees: Decimal = Decimal("0")
    ) -> Decimal:
        if not all(isinstance(value, Decimal) for value in [quantity, entry_price, exit_price, fees]):
            raise TypeError("Profit/loss inputs must be Decimal")
        return (quantity * exit_price) - (quantity * entry_price) - fees
    
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
        idempotency_key: Optional[str] = None,
        max_slippage_bps: int = 50,
        record_in_ledger: bool = True
    ) -> OrderResult:
        start_time = time.time()
        correlation_id = correlation_id or generate_correlation_id()
        is_paper_trading = getattr(self.client, "paper_trading", False)

        with CorrelationContext.use(correlation_id):
            try:
                # Network partition detection is only meaningful in live mode.
                # In paper mode no real API calls are made so record_success()
                # is never called → the monitor always trips after 15 s → every
                # paper order is blocked.  Skip the check entirely for paper runs.
                if not is_paper_trading and self.network_monitor.check_partition():
                    raise NetworkPartitionError("Trading halted: network partition detected")
                if not token_id or not isinstance(token_id, str):
                    raise ValueError("Invalid token_id")
                market_id = BoundaryValidator.validate_market_id(market_id)
                side_str = BoundaryValidator.validate_side(side)
                quantity = BoundaryValidator.validate_quantity(quantity)
                price = BoundaryValidator.validate_price(price)
            except NetworkPartitionError as e:
                logger.critical(
                    "network_partition_blocked_order",
                    **inject_correlation({"error": str(e), "error_code": ErrorCode.NETWORK_PARTITION.value})
                )
                result = OrderResult(
                    success=False,
                    status=OrderStatus.FAILED,
                    error=str(e),
                    error_code=ErrorCode.NETWORK_PARTITION.value,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False,
                )
                self._idempotency_cache.set(idempotency_key, result)
                return result
            except ValueError as e:
                error_message = str(e)
                if "price" in error_message.lower():
                    error_code = ErrorCode.INVALID_PRICE.value
                elif "quantity" in error_message.lower():
                    error_code = ErrorCode.INVALID_QUANTITY.value
                elif "token_id" in error_message.lower():
                    error_code = ErrorCode.INVALID_ORDER.value
                elif "market_id" in error_message.lower():
                    error_code = ErrorCode.MARKET_NOT_FOUND.value
                else:
                    error_code = ErrorCode.INVALID_STATE.value
                logger.error("invalid_order_request", **inject_correlation({"error": error_message, "error_code": error_code}))
                return OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    error=error_message,
                    error_code=error_code,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False
                )

            idempotency_key = IdempotencyKeyBuilder.build(
                strategy=strategy,
                market_id=market_id,
                token_id=token_id,
                side=side_str,
                quantity=quantity,
                price=price,
                order_type=order_type,
                override_key=idempotency_key
            )

            existing_record = await self._get_idempotency_record(idempotency_key)
            if existing_record is not None:
                status_value = existing_record.get("status") or OrderStatus.PENDING.value
                try:
                    existing_status = OrderStatus(status_value)
                except ValueError:
                    existing_status = OrderStatus.PENDING
                duplicate_result = OrderResult(
                    success=status_value in {OrderStatus.FILLED.value, OrderStatus.SUBMITTED.value, OrderStatus.PENDING.value},
                    order_id=existing_record.get("order_id"),
                    status=existing_status,
                    filled_quantity=existing_record.get("filled_quantity", Decimal("0")),
                    filled_price=existing_record.get("filled_price", Decimal("0")),
                    fees=existing_record.get("fees", Decimal("0")),
                    error=None,
                    error_code=None,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=True
                )
                self._idempotency_cache.set(idempotency_key, duplicate_result)
                logger.info(
                    "order_deduplicated_db_hit",
                    **inject_correlation({
                        "idempotency_key": idempotency_key,
                        "order_id": duplicate_result.order_id,
                        "status": duplicate_result.status.value
                    })
                )
                return duplicate_result

            cached_result = self._idempotency_cache.get(idempotency_key)
            if cached_result is not None:
                cached_payload = dict(cached_result)
                cached_payload.update(
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=True
                )
                cached_copy = OrderResult(**cached_payload)
                logger.info(
                    "order_deduplicated_cache_hit",
                    **inject_correlation({
                        "idempotency_key": idempotency_key,
                        "cached_order_id": cached_copy.order_id,
                        "cached_success": cached_copy.success,
                        "strategy": strategy,
                        "market_id": market_id,
                        "side": side_str,
                        "quantity": str(quantity),
                        "price": str(price)
                    })
                )
                return cached_copy

            try:
                order_side = OrderSide.SELL if side_str == "SELL" else OrderSide.BUY
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
                error_message = str(e)
                if "price" in error_message.lower():
                    error_code = ErrorCode.INVALID_PRICE.value
                elif "quantity" in error_message.lower():
                    error_code = ErrorCode.INVALID_QUANTITY.value
                else:
                    error_code = ErrorCode.INVALID_STATE.value
                logger.error("invalid_order_request", **inject_correlation({"error": error_message, "error_code": error_code}))
                result = OrderResult(
                    success=False,
                    status=OrderStatus.REJECTED,
                    error=error_message,
                    error_code=error_code,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False
                )
                self._idempotency_cache.set(idempotency_key, result)
                return result

            async def submit_order() -> str:
                try:
                    response = await self.client.place_order(
                        token_id=token_id,
                        side=order_side,
                        price=price,
                        size=quantity,
                        order_type=order_type,
                        market_id=market_id,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key
                    )
                except Exception as exc:
                    raise OperationalError(
                        ErrorCode.NETWORK_ERROR,
                        str(exc),
                        metadata={"stage": "client.place_order"}
                    ) from exc

                if isinstance(response, OrderResult):
                    if not response.success:
                        error_code = self._resolve_error_code(response.error_code)
                        error_message = response.error or "Order submission failed"
                        if error_code in RETRYABLE_CODES:
                            raise OperationalError(error_code, error_message)
                        raise ValidationError(error_code, error_message)
                    self.network_monitor.record_success()
                    return response.order_id

                if not response or not isinstance(response, dict) or not response.get("success"):
                    error_message = response.get("error") if isinstance(response, dict) else "Order submission failed"
                    error_code_value = response.get("error_code") if isinstance(response, dict) else ErrorCode.ORDER_SUBMISSION_FAILED.value
                    error_code = self._resolve_error_code(error_code_value)
                    if error_code in RETRYABLE_CODES:
                        raise OperationalError(error_code, error_message)
                    raise ValidationError(error_code, error_message)
                self.network_monitor.record_success()
                return response["order_id"]

            try:
                order_id = await RetryableOperation.run(
                    submit_order,
                    max_retries=self.max_retries
                )
            except NetworkPartitionError as e:
                logger.critical(
                    "network_partition_blocked_order",
                    **inject_correlation({"error": str(e), "error_code": ErrorCode.NETWORK_PARTITION.value})
                )
                result = OrderResult(
                    success=False,
                    status=OrderStatus.FAILED,
                    error=str(e),
                    error_code=ErrorCode.NETWORK_PARTITION.value,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False,
                )
                self._idempotency_cache.set(idempotency_key, result)
                return result
            except TradingException as e:
                status = OrderStatus.REJECTED if isinstance(e, ValidationError) else OrderStatus.FAILED
                logger.error(
                    "order_execution_error",
                    **inject_correlation({
                        "error": str(e),
                        "error_code": e.code.value,
                        "retryable": e.retryable
                    })
                )
                self.orders_failed += 1
                await self._record_order_audit(
                    order_id=idempotency_key,
                    old_state=None,
                    new_state=status.value,
                    reason="submission_failed",
                    context=e.to_dict(),
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key
                )
                result = OrderResult(
                    success=False,
                    status=status,
                    error=str(e),
                    error_code=e.code.value,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False
                )
                self._idempotency_cache.set(idempotency_key, result)
                return result
            except Exception as e:
                logger.error(
                    "order_execution_error",
                    **inject_correlation({
                        "error": str(e),
                        "error_code": ErrorCode.UNKNOWN.value
                    })
                )
                self.orders_failed += 1
                result = OrderResult(
                    success=False,
                    status=OrderStatus.FAILED,
                    error=str(e),
                    error_code=ErrorCode.UNKNOWN.value,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                    is_duplicate=False
                )
                self._idempotency_cache.set(idempotency_key, result)
                return result

            await self._record_idempotency(
                idempotency_key=idempotency_key,
                order_id=order_id,
                correlation_id=correlation_id,
                status=OrderStatus.PENDING.value
            )

            order_state = OrderState(request, order_id)
            await self._record_order_audit(
                order_id=order_id,
                old_state=None,
                new_state=OrderStatus.PENDING.value,
                reason="order_created",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )
            previous_status = order_state.status
            order_state.mark_submitted()
            await self._record_order_audit(
                order_id=order_id,
                old_state=previous_status.value,
                new_state=order_state.status.value,
                reason="order_submitted",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )

            async with self.order_lock:
                self.orders[order_id] = order_state

            self.orders_placed += 1

            if is_paper_trading:
                fill = Fill(
                    fill_id=f"fill_{order_id}",
                    order_id=order_id,
                    quantity=quantity,
                    price=price,
                    fee=quantity * price * Decimal("0.002"),
                    timestamp=datetime.now(timezone.utc)
                )
                previous_status = order_state.status
                order_state.add_fill(fill)
                if previous_status != order_state.status:
                    await self._record_order_audit(
                        order_id=order_id,
                        old_state=previous_status.value,
                        new_state=order_state.status.value,
                        reason="paper_fill",
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key
                    )

                logger.info(
                    "paper_fill_simulated",
                    **inject_correlation({
                        "order_id": order_id,
                        "quantity": str(quantity),
                        "price": str(price)
                    })
                )
            else:
                fill_success = await self._wait_for_fills(
                    order_state,
                    timeout=to_timeout_float(self.timeout_seconds),
                    target_price=price,
                    max_slippage_bps=max_slippage_bps,
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key
                )
                if not fill_success and order_state.status == OrderStatus.FAILED:
                    result = OrderResult(
                        success=False,
                        order_id=order_id,
                        status=order_state.status,
                        filled_quantity=order_state.filled_quantity,
                        filled_price=order_state.avg_fill_price,
                        fees=order_state.total_fees,
                        error="slippage_violation",
                        error_code=ErrorCode.SLIPPAGE_VIOLATION.value,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                        is_duplicate=False,
                    )
                    self._idempotency_cache.set(idempotency_key, result)
                    return result

            execution_time_ms = (time.time() - start_time) * 1000
            self.execution_times_ms.append(execution_time_ms)

            slippage_bps = Decimal("0")
            if order_state.avg_fill_price > 0:
                slippage_bps = (order_state.avg_fill_price - price) / price * Decimal("10000")
                self.total_slippage_bps += abs(slippage_bps)

                # --- Auto-block on excessive slippage -------------------------
                # If realised slippage exceeds the configured threshold, add the
                # market to the do-not-trade list.  This catches venues that
                # consistently fill at unfavourable prices before the next manual
                # review.  Threshold defaults to 200 bps (configurable via
                # execution.auto_block_slippage_bps).
                if (
                    self._do_not_trade_registry is not None
                    and abs(slippage_bps) > self._auto_block_slippage_bps
                ):
                    reason = (
                        f"auto_slippage_{abs(slippage_bps):.0f}bps"
                        f"_threshold_{self._auto_block_slippage_bps:.0f}bps"
                    )
                    logger.warning(
                        "auto_block_excessive_slippage",
                        market_id=market_id,
                        slippage_bps=str(slippage_bps),
                        threshold_bps=str(self._auto_block_slippage_bps),
                        reason=reason,
                    )
                    self._do_not_trade_registry.block(
                        market_id,
                        reason=reason,
                        auto=True,
                    )

            self.total_fees += order_state.total_fees

            if order_state.filled_quantity > 0:
                if record_in_ledger:
                    await self._record_trade_in_ledger(
                        order_state,
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key
                    )
                if order_state.status == OrderStatus.FILLED:
                    self.orders_filled += 1

            await self._update_idempotency(
                idempotency_key=idempotency_key,
                status=order_state.status.value,
                filled_quantity=order_state.filled_quantity,
                filled_price=order_state.avg_fill_price,
                fees=order_state.total_fees
            )

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
                **inject_correlation({
                    "order_id": order_id,
                    "status": order_state.status.value,
                    "filled_quantity": str(order_state.filled_quantity),
                    "avg_price": str(order_state.avg_fill_price),
                    "fees": str(order_state.total_fees),
                    "slippage_bps": str(slippage_bps),
                    "execution_time_ms": execution_time_ms
                })
            )

            self._idempotency_cache.set(idempotency_key, result)
            return result
    
    async def _get_idempotency_record(self, idempotency_key: str) -> Optional[Dict]:
        if not hasattr(self.ledger, "get_idempotency_record"):
            return None
        return await self.ledger.get_idempotency_record(idempotency_key)

    async def _record_idempotency(
        self,
        idempotency_key: str,
        order_id: str,
        correlation_id: Optional[str],
        status: str
    ) -> None:
        if not hasattr(self.ledger, "record_idempotency"):
            return
        await self.ledger.record_idempotency(
            idempotency_key=idempotency_key,
            order_id=order_id,
            correlation_id=correlation_id,
            status=status
        )

    async def _update_idempotency(
        self,
        idempotency_key: str,
        status: str,
        filled_quantity: Decimal,
        filled_price: Decimal,
        fees: Decimal
    ) -> None:
        if not hasattr(self.ledger, "update_idempotency"):
            return
        await self.ledger.update_idempotency(
            idempotency_key=idempotency_key,
            status=status,
            filled_quantity=filled_quantity,
            filled_price=filled_price,
            fees=fees
        )

    async def _wait_for_fills(
        self,
        order_state: OrderState,
        timeout: float,
        target_price: Decimal,
        max_slippage_bps: int,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> bool:
        start_time = time.time()

        max_slippage = Decimal(str(max_slippage_bps)) / Decimal("10000")
        max_fill_price = target_price * (Decimal("1") + max_slippage)
        min_fill_price = target_price * (Decimal("1") - max_slippage)
        
        while (time.time() - start_time) < timeout:
            status_response = await self.client.get_order_status(order_state.order_id)
            if status_response:
                self.network_monitor.record_success()
            
            if status_response:
                fills = status_response.get('fills', [])
                for fill_data in fills:
                    fill_id = fill_data.get('id')
                    if not any(f.fill_id == fill_id for f in order_state.fills):
                        previous_status = order_state.status
                        fill = Fill(
                            fill_id=fill_id,
                            order_id=order_state.order_id,
                            quantity=Decimal(str(fill_data.get('size', 0))),
                            price=Decimal(str(fill_data.get('price', 0))),
                            fee=Decimal(str(fill_data.get('fee', 0))),
                            timestamp=datetime.now(timezone.utc)
                        )
                        if fill.price > max_fill_price or fill.price < min_fill_price:
                            logger.error(
                                "slippage_violation",
                                **inject_correlation({
                                    "order_id": order_state.order_id,
                                    "fill_price": str(fill.price),
                                    "target_price": str(target_price),
                                    "max_slippage_bps": max_slippage_bps,
                                })
                            )
                            await self.client.cancel_order(order_state.order_id)
                            previous_status = order_state.status
                            order_state.status = OrderStatus.FAILED
                            await self._record_order_audit(
                                order_id=order_state.order_id,
                                old_state=previous_status.value,
                                new_state=order_state.status.value,
                                reason="slippage_violation",
                                correlation_id=correlation_id,
                                idempotency_key=idempotency_key,
                            )
                            return False
                        order_state.add_fill(fill)

                        if previous_status != order_state.status:
                            await self._record_order_audit(
                                order_id=order_state.order_id,
                                old_state=previous_status.value,
                                new_state=order_state.status.value,
                                reason="fill_update",
                                correlation_id=order_state.request.metadata.get("correlation_id") if order_state.request.metadata else None,
                                idempotency_key=order_state.request.metadata.get("idempotency_key") if order_state.request.metadata else None,
                            )
                        
                        logger.info(
                            "fill_received",
                            **inject_correlation({
                                "order_id": order_state.order_id,
                                "fill_id": fill_id,
                                "quantity": str(fill.quantity),
                                "price": str(fill.price)
                            })
                        )
                
                if order_state.status == OrderStatus.FILLED:
                    return True
            
            await asyncio.sleep(to_timeout_float(self.fill_check_interval))
        
        if order_state.status not in [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED]:
            previous_status = order_state.status
            order_state.status = OrderStatus.EXPIRED
            logger.warning(
                "order_expired",
                **inject_correlation({
                    "order_id": order_state.order_id,
                    "age_seconds": order_state.age_seconds
                })
            )
            await self._record_order_audit(
                order_id=order_state.order_id,
                old_state=previous_status.value,
                new_state=order_state.status.value,
                reason="order_expired",
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
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
                order_id=order_state.order_id,
                market_id=order_state.request.market_id,
                token_id=order_state.request.token_id,
                strategy=order_state.request.strategy,
                side=order_state.request.side.value if hasattr(order_state.request.side, "value") else str(order_state.request.side),
                quantity=order_state.filled_quantity,
                price=order_state.avg_fill_price,
                correlation_id=correlation_id,
                metadata={
                    **(order_state.request.metadata or {}),
                    "correlation_id": correlation_id,
                    "idempotency_key": idempotency_key,
                }
            )

            equity = await self.ledger.get_equity()
            self.precision_monitor.check_equity(equity, correlation_id=correlation_id)
            
            logger.info(
                "trade_recorded_in_ledger",
                **inject_correlation({
                    "position_id": position_id,
                    "order_id": order_state.order_id,
                    "idempotency_key": idempotency_key
                })
            )
        
        except PrecisionError as e:
            logger.error(
                "precision_monitor_failed",
                **inject_correlation({
                    "order_id": order_state.order_id,
                    "error": str(e),
                    "error_code": ErrorCode.INVALID_STATE.value
                })
            )
            self.dlq.append(order_state)

    async def close_position(
        self,
        position_id: int,
        exit_reason: str,
        exit_price: Optional[Decimal] = None,
        max_slippage_bps: int = 50,
    ) -> OrderResult:
        correlation_id = generate_correlation_id()

        positions = await self.ledger.get_open_positions()
        position = next((p for p in positions if p.id == position_id), None)
        if not position:
            return OrderResult(
                success=False,
                order_id=None,
                status=OrderStatus.FAILED,
                error="position_not_found",
                error_code=ErrorCode.INVALID_STATE.value,
                correlation_id=correlation_id,
            )

        if exit_price is None:
            orderbook = await self.client.get_orderbook(position.token_id)
            if orderbook:
                bids = orderbook.get("bids", [])
                asks = orderbook.get("asks", [])
                try:
                    bid = Decimal(str(bids[0].get("price"))) if bids else position.entry_price
                    ask = Decimal(str(asks[0].get("price"))) if asks else position.entry_price
                    exit_price = (bid + ask) / Decimal("2")
                except Exception:
                    exit_price = position.entry_price
            else:
                exit_price = position.entry_price

        result = await self.place_order(
            strategy=position.strategy,
            market_id=position.market_id,
            token_id=position.token_id,
            side="SELL",
            quantity=position.quantity,
            price=exit_price,
            order_type="FOK",
            metadata={"exit_reason": exit_reason, "position_id": position_id},
            correlation_id=correlation_id,
            max_slippage_bps=max_slippage_bps,
            record_in_ledger=False,
        )

        if result.success and result.filled_quantity > 0:
            await self.ledger.record_trade_exit(
                position_id=position_id,
                exit_price=result.filled_price,
                fees=result.fees,
                exit_reason=exit_reason,
                correlation_id=correlation_id,
                exit_order_id=result.order_id,
            )

        return result
    
    async def _fill_monitor_loop(self):
        logger.info("fill_monitor_started")
        
        while True:
            try:
                await asyncio.sleep(to_timeout_float(self.fill_check_interval))
                
                async with self.order_lock:
                    pending_orders = [
                        order for order in self.orders.values()
                        if order.status in [
                            OrderStatus.SUBMITTED,
                            OrderStatus.PARTIALLY_FILLED
                        ]
                    ]
                
                for order in pending_orders:
                    await self._wait_for_fills(
                        order,
                        timeout=to_timeout_float(Decimal("0.1")),
                        target_price=order.request.price,
                        max_slippage_bps=50,
                        correlation_id=order.request.metadata.get("correlation_id") if order.request.metadata else None,
                        idempotency_key=order.request.metadata.get("idempotency_key") if order.request.metadata else None,
                    )
            
            except asyncio.CancelledError:
                logger.info("fill_monitor_stopped")
                break
            except Exception as e:
                logger.error("fill_monitor_error", error=str(e))
    
    async def _cleanup_loop(self):
        logger.info("cleanup_loop_started")
        
        while True:
            try:
                await asyncio.sleep(to_timeout_float(60))
                
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

    async def cleanup_old_orders(self, max_age_seconds: int = 3600) -> int:
        """Remove completed orders older than the provided age threshold."""
        async with self.order_lock:
            stale_order_ids = [
                order_id
                for order_id, order in self.orders.items()
                if order.is_complete and order.age_seconds > max_age_seconds
            ]

            for order_id in stale_order_ids:
                del self.orders[order_id]

        if stale_order_ids:
            logger.info("orders_cleaned_up", count=len(stale_order_ids), source="manual_cleanup")

        return len(stale_order_ids)
    
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
            if self.orders_filled > 0 else Decimal("0")
        )
        
        return {
            "orders_placed": self.orders_placed,
            "orders_filled": self.orders_filled,
            "orders_failed": self.orders_failed,
            "fill_rate": fill_rate,
            "avg_execution_time_ms": avg_execution_time,
            "avg_slippage_bps": avg_slippage,
            "total_fees": self.total_fees,
            "active_orders": len(self.orders),
            "dlq_size": len(self.dlq)
        }
