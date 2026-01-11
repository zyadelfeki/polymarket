#!/usr/bin/env python3
"""
Institutional-Grade Input Validation Models

Features:
- Pydantic models for all inputs
- API response validation
- Type coercion and validation
- Custom validators for business logic
- Clear error messages
- JSON serialization

Standards:
- Zero invalid data enters system
- All inputs validated at boundary
- Clear, actionable error messages
- Type-safe throughout
"""

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings
from typing import Optional, List, Dict, Any, Literal
from decimal import Decimal
from datetime import datetime
from enum import Enum
import re


# ==================== ENUMS ====================

class OrderSide(str, Enum):
    """Order side enum"""
    BUY = "BUY"
    SELL = "SELL"
    YES = "YES"
    NO = "NO"


class OrderType(str, Enum):
    """Order type enum"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    FOK = "FOK"  # Fill or Kill
    GTT = "GTT"  # Good til Time


class OrderStatus(str, Enum):
    """Order status enum"""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionStatus(str, Enum):
    """Position status enum"""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"


# ==================== ORDER MODELS ====================

class OrderRequest(BaseModel):
    """
    Order placement request.
    
    Validates all order parameters before submission.
    """
    strategy: str = Field(..., min_length=1, max_length=50)
    market_id: str = Field(..., min_length=1, max_length=100)
    token_id: str = Field(..., min_length=1, max_length=100)
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    quantity: Decimal = Field(..., gt=0, le=100000)
    price: Optional[Decimal] = Field(None, gt=0, le=0.99)
    
    # Optional fields
    metadata: Optional[Dict[str, Any]] = None
    
    @field_validator('price')
    @classmethod
    def validate_price(cls, v: Optional[Decimal], info) -> Optional[Decimal]:
        """Validate price is in valid range for prediction markets."""
        if v is not None:
            if v <= Decimal('0.01'):
                raise ValueError('Price must be > 0.01 (1 cent)')
            if v >= Decimal('0.99'):
                raise ValueError('Price must be < 0.99 (99 cents)')
        return v
    
    @field_validator('quantity')
    @classmethod
    def validate_quantity(cls, v: Decimal) -> Decimal:
        """Validate quantity is reasonable."""
        if v < Decimal('0.01'):
            raise ValueError('Quantity must be >= 0.01')
        if v > Decimal('100000'):
            raise ValueError('Quantity exceeds maximum (100,000)')
        return v
    
    @field_validator('market_id', 'token_id')
    @classmethod
    def validate_ids(cls, v: str) -> str:
        """Validate IDs are not empty and have valid format."""
        if not v or not v.strip():
            raise ValueError('ID cannot be empty')
        # Basic sanity check - no obvious injection attempts
        if any(char in v for char in ['<', '>', ';', '\x00']):
            raise ValueError('ID contains invalid characters')
        return v.strip()
    
    @model_validator(mode='after')
    def validate_order_consistency(self):
        """Validate order internal consistency."""
        # LIMIT orders must have price
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError('LIMIT orders require a price')
        
        # MARKET orders should not have price
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError('MARKET orders do not accept price')
        
        return self


class OrderResponse(BaseModel):
    """Order placement response from API."""
    success: bool
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    message: Optional[str] = None
    error: Optional[str] = None
    
    # Execution details
    filled_quantity: Decimal = Decimal('0')
    average_fill_price: Optional[Decimal] = None
    fees: Decimal = Decimal('0')
    
    # Timestamps
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    
    @field_validator('order_id')
    @classmethod
    def validate_order_id(cls, v: Optional[str]) -> Optional[str]:
        """Validate order ID format."""
        if v is not None and (not v or not v.strip()):
            raise ValueError('Order ID cannot be empty string')
        return v


# ==================== MARKET MODELS ====================

class MarketData(BaseModel):
    """Market data snapshot."""
    market_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1, max_length=500)
    
    # Status
    active: bool = True
    closed: bool = False
    resolved: bool = False
    
    # Tokens
    tokens: List[Dict[str, str]] = Field(default_factory=list)
    
    # Pricing
    current_price: Optional[Decimal] = Field(None, ge=0, le=1)
    volume_24h: Decimal = Field(default=Decimal('0'), ge=0)
    
    # Metadata
    created_at: Optional[datetime] = None
    close_time: Optional[datetime] = None
    
    @field_validator('tokens')
    @classmethod
    def validate_tokens(cls, v: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Validate token structure."""
        if not v:
            raise ValueError('Market must have at least one token')
        
        for token in v:
            if 'token_id' not in token:
                raise ValueError('Token missing token_id')
            if 'outcome' not in token:
                raise ValueError('Token missing outcome')
        
        return v


class OrderBook(BaseModel):
    """Order book data."""
    market_id: str
    token_id: str
    
    bids: List[Dict[str, str]] = Field(default_factory=list)
    asks: List[Dict[str, str]] = Field(default_factory=list)
    
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    @field_validator('bids', 'asks')
    @classmethod
    def validate_orders(cls, v: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Validate order book entries."""
        for order in v:
            if 'price' not in order or 'size' not in order:
                raise ValueError('Order book entry must have price and size')
            
            # Validate price
            try:
                price = Decimal(order['price'])
                if price <= 0 or price >= 1:
                    raise ValueError(f'Invalid price: {price}')
            except (ValueError, TypeError):
                raise ValueError(f'Invalid price format: {order["price"]}')
            
            # Validate size
            try:
                size = Decimal(order['size'])
                if size <= 0:
                    raise ValueError(f'Invalid size: {size}')
            except (ValueError, TypeError):
                raise ValueError(f'Invalid size format: {order["size"]}')
        
        return v


# ==================== PRICE MODELS ====================

class PriceUpdate(BaseModel):
    """Price update from WebSocket."""
    symbol: str = Field(..., min_length=1, max_length=20)
    price: Decimal = Field(..., gt=0)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    # Optional fields
    change_24h: Optional[float] = None
    volume_24h: Optional[float] = Field(None, ge=0)
    high_24h: Optional[Decimal] = Field(None, gt=0)
    low_24h: Optional[Decimal] = Field(None, gt=0)
    
    @field_validator('symbol')
    @classmethod
    def validate_symbol(cls, v: str) -> str:
        """Validate symbol format."""
        # Allow only alphanumeric and common separators
        if not re.match(r'^[A-Z0-9_-]+$', v.upper()):
            raise ValueError(f'Invalid symbol format: {v}')
        return v.upper()
    
    @model_validator(mode='after')
    def validate_high_low(self):
        """Validate high >= low."""
        if self.high_24h is not None and self.low_24h is not None:
            if self.high_24h < self.low_24h:
                raise ValueError('High price cannot be less than low price')
        return self


# ==================== POSITION MODELS ====================

class PositionEntry(BaseModel):
    """Position entry request."""
    market_id: str = Field(..., min_length=1)
    token_id: str = Field(..., min_length=1)
    strategy: str = Field(..., min_length=1, max_length=50)
    
    entry_price: Decimal = Field(..., gt=0, le=0.99)
    quantity: Decimal = Field(..., gt=0)
    fees: Decimal = Field(default=Decimal('0'), ge=0)
    
    order_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class PositionData(BaseModel):
    """Position data."""
    id: int
    market_id: str
    token_id: str
    strategy: str
    
    # Entry
    entry_price: Decimal
    quantity: Decimal
    entry_timestamp: datetime
    
    # Current state
    current_price: Optional[Decimal] = None
    status: PositionStatus = PositionStatus.OPEN
    
    # P&L
    unrealized_pnl: Decimal = Decimal('0')
    realized_pnl: Decimal = Decimal('0')
    fees_paid: Decimal = Decimal('0')
    
    # Exit (if closed)
    exit_price: Optional[Decimal] = None
    exit_timestamp: Optional[datetime] = None
    
    # Metadata
    metadata: Optional[Dict[str, Any]] = None


# ==================== TRANSACTION MODELS ====================

class TransactionRequest(BaseModel):
    """Transaction recording request."""
    description: str = Field(..., min_length=1, max_length=200)
    transaction_type: Literal[
        'DEPOSIT', 'WITHDRAWAL', 'TRADE_ENTRY', 
        'TRADE_EXIT', 'FEE', 'ADJUSTMENT'
    ]
    reference_id: Optional[str] = Field(None, max_length=100)
    
    # Transaction lines
    lines: List[Dict[str, Any]] = Field(..., min_items=2)
    
    @model_validator(mode='after')
    def validate_balanced(self):
        """Validate transaction is balanced (debits = credits)."""
        total = Decimal('0')
        
        for line in self.lines:
            if 'amount' not in line:
                raise ValueError('Transaction line missing amount')
            
            try:
                amount = Decimal(str(line['amount']))
                total += amount
            except (ValueError, TypeError):
                raise ValueError(f'Invalid amount: {line["amount"]}')
        
        # Check if balanced (within rounding tolerance)
        if abs(total) > Decimal('0.01'):
            raise ValueError(f'Transaction not balanced: {total}')
        
        return self


# ==================== CONFIGURATION MODELS ====================

class TradingConfig(BaseSettings):
    """Trading configuration with validation."""
    
    # Account
    initial_capital: Decimal = Field(default=Decimal('10000'), gt=0)
    max_position_size_pct: float = Field(default=10.0, gt=0, le=100)
    max_total_exposure_pct: float = Field(default=80.0, gt=0, le=100)
    
    # Risk management
    max_drawdown_pct: float = Field(default=15.0, gt=0, lt=100)
    daily_loss_limit_pct: float = Field(default=10.0, gt=0, lt=100)
    max_loss_streak: int = Field(default=5, gt=0, le=20)
    
    # Order limits
    min_order_size: Decimal = Field(default=Decimal('1.0'), gt=0)
    max_order_size: Decimal = Field(default=Decimal('1000.0'), gt=0)
    min_price: Decimal = Field(default=Decimal('0.01'), gt=0, lt=1)
    max_price: Decimal = Field(default=Decimal('0.99'), gt=0, lt=1)
    
    # Rate limits
    max_orders_per_minute: int = Field(default=30, gt=0, le=1000)
    max_orders_per_hour: int = Field(default=500, gt=0, le=10000)
    
    # API
    api_rate_limit: float = Field(default=8.0, gt=0, le=100)
    api_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    
    # Monitoring
    health_check_interval: float = Field(default=30.0, gt=0, le=3600)
    
    @model_validator(mode='after')
    def validate_consistency(self):
        """Validate configuration internal consistency."""
        # Max position < max exposure
        if self.max_position_size_pct > self.max_total_exposure_pct:
            raise ValueError(
                'max_position_size_pct cannot exceed max_total_exposure_pct'
            )
        
        # Min order < max order
        if self.min_order_size >= self.max_order_size:
            raise ValueError('min_order_size must be < max_order_size')
        
        # Min price < max price
        if self.min_price >= self.max_price:
            raise ValueError('min_price must be < max_price')
        
        return self
    
    class Config:
        env_prefix = 'TRADING_'


# ==================== API CREDENTIALS ====================

class APICredentials(BaseSettings):
    """API credentials with validation."""
    
    # Polymarket
    polymarket_api_key: Optional[str] = Field(None, min_length=10)
    polymarket_api_secret: Optional[str] = Field(None, min_length=10)
    polymarket_private_key: Optional[str] = Field(None, min_length=64)
    
    # Paper trading
    paper_trading: bool = Field(default=True)
    
    @field_validator('polymarket_private_key')
    @classmethod
    def validate_private_key(cls, v: Optional[str]) -> Optional[str]:
        """Validate private key format."""
        if v is not None:
            # Remove 0x prefix if present
            v = v.replace('0x', '').replace('0X', '')
            
            # Check if valid hex
            if not re.match(r'^[0-9a-fA-F]{64}$', v):
                raise ValueError('Private key must be 64 hex characters')
            
            return '0x' + v
        return v
    
    @model_validator(mode='after')
    def validate_credentials(self):
        """Validate credentials are complete for trading mode."""
        if not self.paper_trading:
            if not self.polymarket_private_key:
                raise ValueError(
                    'polymarket_private_key required for live trading'
                )
        return self
    
    class Config:
        env_prefix = ''
        env_file = '.env'


# ==================== VALIDATION UTILITIES ====================

def validate_market_id(market_id: str) -> bool:
    """Validate market ID format."""
    if not market_id or not market_id.strip():
        return False
    if len(market_id) > 100:
        return False
    # No obvious injection attempts
    if any(char in market_id for char in ['<', '>', ';', '\x00']):
        return False
    return True


def validate_price_range(price: Decimal) -> bool:
    """Validate price is in valid range for prediction markets."""
    return Decimal('0.01') <= price <= Decimal('0.99')


def validate_quantity(quantity: Decimal, max_quantity: Decimal = Decimal('100000')) -> bool:
    """Validate quantity is reasonable."""
    return Decimal('0.01') <= quantity <= max_quantity


def validate_order_request(request: Dict[str, Any]) -> OrderRequest:
    """
    Validate and parse order request.
    
    Args:
        request: Raw request dictionary
    
    Returns:
        Validated OrderRequest
    
    Raises:
        ValidationError: If validation fails
    """
    return OrderRequest(**request)


def validate_trading_config(config: Dict[str, Any]) -> TradingConfig:
    """
    Validate and parse trading configuration.
    
    Args:
        config: Raw config dictionary
    
    Returns:
        Validated TradingConfig
    
    Raises:
        ValidationError: If validation fails
    """
    return TradingConfig(**config)


# ==================== EXAMPLE USAGE ====================

if __name__ == '__main__':
    # Example: Validate order request
    try:
        order = OrderRequest(
            strategy='latency_arb',
            market_id='market_btc_100k',
            token_id='token_yes',
            side=OrderSide.YES,
            order_type=OrderType.LIMIT,
            quantity=Decimal('100'),
            price=Decimal('0.55')
        )
        print(f"✅ Valid order: {order.model_dump_json()}")
    except Exception as e:
        print(f"❌ Invalid order: {e}")
    
    # Example: Validate configuration
    try:
        config = TradingConfig(
            initial_capital=Decimal('10000'),
            max_drawdown_pct=15.0,
            max_position_size_pct=10.0
        )
        print(f"✅ Valid config: {config.model_dump()}")
    except Exception as e:
        print(f"❌ Invalid config: {e}")
