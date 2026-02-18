from decimal import Decimal
from typing import Literal, Optional, TypedDict


class OrderResult(TypedDict):
    success: bool
    order_id: Optional[str]
    error: Optional[str]
    filled_size: Optional[Decimal]
    avg_price: Optional[Decimal]
    timestamp: float


OrderSide = Literal["BUY", "SELL"]
OrderOutcome = Literal["YES", "NO"]
