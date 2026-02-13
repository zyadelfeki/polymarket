import time
from decimal import Decimal, getcontext
from collections import deque
from typing import Optional

getcontext().prec = 18


class PriceHistory:
    """Store price ticks for interval start lookups."""

    def __init__(self, max_age_seconds: int = 3600) -> None:
        self.prices = deque(maxlen=7200)
        self.max_age = max_age_seconds

    def record_price(self, symbol: str, price: Decimal) -> None:
        self.prices.append(
            {
                "symbol": symbol,
                "price": Decimal(str(price)),
                "timestamp": time.time(),
            }
        )

    def get_price_at_time(self, symbol: str, target_timestamp: float) -> Optional[Decimal]:
        closest = None
        min_diff = float("inf")

        for entry in self.prices:
            if entry["symbol"] != symbol:
                continue

            diff = abs(entry["timestamp"] - target_timestamp)
            if diff < min_diff and diff <= 5.0:
                min_diff = diff
                closest = entry

        return Decimal(str(closest["price"])) if closest else None
