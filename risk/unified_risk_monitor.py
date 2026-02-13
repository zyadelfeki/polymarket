from decimal import Decimal, getcontext
from typing import Dict
import json
from pathlib import Path

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


class UnifiedRiskAggregator:
    """
    Aggregate risk across Charlie (crypto bot) and Polymarket bot.
    """

    def __init__(
        self,
        max_total_exposure: Decimal = Decimal("1000.00"),
        shared_data_dir: str = "./shared_data",
    ):
        self.max_total_exposure = max_total_exposure
        self.shared_dir = Path(shared_data_dir)
        self.crypto_positions_file = self.shared_dir / "crypto_positions.json"
        self.polymarket_positions_file = self.shared_dir / "polymarket_positions.json"

    def calculate_total_delta(self) -> Decimal:
        crypto_delta = self._load_crypto_positions()
        polymarket_delta = self._load_polymarket_positions()

        total = crypto_delta + polymarket_delta

        logger.info(
            "total_risk_exposure",
            crypto=str(crypto_delta),
            polymarket=str(polymarket_delta),
            total=str(total),
            limit=str(self.max_total_exposure),
        )

        return total

    def _load_crypto_positions(self) -> Decimal:
        if not self.crypto_positions_file.exists():
            return Decimal("0")

        with open(self.crypto_positions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        total = Decimal("0")
        for pos in data.get("positions", []):
            if pos.get("symbol") == "BTC":
                total += Decimal(str(pos.get("size", "0")))

        return total

    def _load_polymarket_positions(self) -> Decimal:
        if not self.polymarket_positions_file.exists():
            return Decimal("0")

        with open(self.polymarket_positions_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        total = Decimal("0")
        for pos in data.get("positions", []):
            question = str(pos.get("question", "")).lower()
            if "btc" in question:
                if pos.get("outcome") == "YES":
                    total += Decimal(str(pos.get("investment", "0")))
                elif pos.get("outcome") == "NO":
                    total -= Decimal(str(pos.get("investment", "0")))

        return total

    def can_place_trade(self, trade_delta: Decimal) -> bool:
        current_delta = self.calculate_total_delta()
        proposed_delta = current_delta + trade_delta

        if abs(proposed_delta) > self.max_total_exposure:
            logger.error(
                "risk_limit_exceeded",
                current=str(current_delta),
                trade=str(trade_delta),
                proposed=str(proposed_delta),
                limit=str(self.max_total_exposure),
            )
            return False

        logger.info("trade_within_risk_limits")
        return True
