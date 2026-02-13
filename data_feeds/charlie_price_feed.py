import json
import time
from pathlib import Path
from decimal import Decimal, getcontext
from typing import Optional

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

getcontext().prec = 18

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


class CharliePriceFeed:
    def __init__(self, data_dir: str = "./shared_data", staleness_threshold: float = 1.0):
        self.price_file = Path(data_dir) / "btc_price.json"
        self.staleness_threshold = staleness_threshold
        self.circuit_breaker_active = False

    def get_price(self, symbol: str) -> Optional[Decimal]:
        if not self.price_file.exists():
            logger.warning("charlie_price_file_missing")
            self.circuit_breaker_active = True
            return None

        try:
            with open(self.price_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            age = time.time() - data.get("timestamp", 0)
            if age > self.staleness_threshold:
                logger.error("stale_charlie_price", age=age, threshold=self.staleness_threshold)
                self.circuit_breaker_active = True
                return None

            price = Decimal(str(data.get("price")))
            logger.debug("charlie_price", symbol=symbol, price=str(price), age=age)
            return price

        except Exception as exc:
            logger.error("charlie_price_read_failed", error=str(exc))
            self.circuit_breaker_active = True
            return None
