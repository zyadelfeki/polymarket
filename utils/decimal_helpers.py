"""
Institutional-grade decimal helpers for financial precision.
Prevents capital leakage through floating-point errors.
"""

from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Union

getcontext().prec = 18


def safe_decimal(value: Any) -> Decimal:
    """Safely convert numeric input to Decimal with float warning."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            try:
                import structlog

                logger = structlog.get_logger(__name__)
                logger.warning(
                    "float_to_decimal_conversion",
                    value=value,
                    precision_warning="Potential precision loss",
                )
            except Exception:
                pass
        return Decimal(str(value))
    raise TypeError(f"Cannot convert {type(value)} to Decimal")


def to_decimal(value: Any) -> Decimal:
    """Backward-compatible Decimal conversion."""
    return safe_decimal(value)


def quantize_price(value: Decimal) -> Decimal:
    """Round to 4 decimal places (Polymarket standard)."""
    return value.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)


def quantize_usdc(value: Decimal) -> Decimal:
    """Round to 2 decimal places (USDC cents)."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def quantize_quantity(value: Decimal) -> Decimal:
    """Round to 2 decimal places (USDC cents)."""
    return quantize_usdc(value)


def quantize_size(value: Decimal, min_size: Decimal = Decimal("0.01")) -> Decimal:
    """
    Token size standard: Market-dependent minimum.
    Rounds down to prevent attempting to trade fractional tokens.
    """
    return value.quantize(min_size, rounding=ROUND_DOWN)


def validate_precision(value: Decimal, max_decimal_places: int = 18) -> bool:
    """
    Validate that a Decimal doesn't exceed precision limits.
    Returns False if value would lose precision in EVM calculations.
    """
    normalized = value.normalize()
    return abs(normalized.as_tuple().exponent) <= max_decimal_places


def to_timeout_float(value: Union[Decimal, float, int]) -> float:
    """Convert to float for asyncio timeouts (non-financial operation)."""
    if isinstance(value, Decimal):
        return value.__float__()
    return float(value)
