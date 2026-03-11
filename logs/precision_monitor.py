"""Precision monitoring utilities."""

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

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

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

    logger = _FallbackLogger(__name__)


class PrecisionError(RuntimeError):
    """Raised when precision corruption is detected."""
    pass


class PrecisionMonitor:
    """Monitor precision for equity values."""

    MAX_DECIMALS = 8
    USD_THRESHOLD = Decimal("0.01")
    QUANT = Decimal("0.00000001")

    @classmethod
    def check_equity(cls, equity: Decimal, correlation_id: Optional[str] = None) -> None:
        if not isinstance(equity, Decimal):
            raise PrecisionError("Equity must be Decimal")

        exponent = equity.as_tuple().exponent
        decimals = abs(exponent) if exponent < 0 else 0
        rounded = equity.quantize(cls.QUANT, rounding=ROUND_HALF_UP)
        delta = abs(equity - rounded)

        if decimals > cls.MAX_DECIMALS:
            logger.warning(
                "precision_excess_detected",
                equity=str(equity),
                decimals=decimals,
                delta=str(delta),
                correlation_id=correlation_id
            )
            if delta > cls.USD_THRESHOLD:
                logger.warning(
                    "precision_excess_threshold",
                    equity=str(equity),
                    delta=str(delta),
                    correlation_id=correlation_id
                )
            raise PrecisionError(
                f"Equity precision corruption detected: {equity}"
            )
