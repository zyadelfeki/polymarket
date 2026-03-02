"""
Institutional-grade decimal helpers for financial precision.
Prevents capital leakage through floating-point errors.
"""

from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any, Union
import warnings

getcontext().prec = 18


def safe_decimal(value: Any) -> Decimal:
    """Safely convert numeric input to Decimal with float warning."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            warnings.warn(
                "float_to_decimal_conversion_detected",
                UserWarning,
                stacklevel=2,
            )
            try:
                import structlog

                logger = structlog.get_logger(__name__)
                logger.warning(
                    "float_to_decimal_conversion_detected",
                    value=value,
                    precision_warning="Potential precision loss",
                )
            except Exception:
                pass
        return Decimal(str(value))
    raise TypeError(f"Cannot convert {type(value)} to Decimal")


def from_config(value: Any) -> Decimal:
    """
    Silent Decimal conversion for config/YAML values.

    YAML loaders return numeric scalars as Python float by design.
    Using safe_decimal() on every config read floods logs with
    float_to_decimal_conversion_detected warnings that carry no signal.
    Use from_config() for all values that come from config dicts/YAML
    and safe_decimal() for runtime financial values where float
    precision should be flagged.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    raise TypeError(f"Cannot convert {type(value)} to Decimal for config read")


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
    if value < min_size:
        return Decimal("0")
    return value.quantize(min_size, rounding=ROUND_DOWN)


def validate_precision(value: Decimal, max_decimal_places: int = 18) -> bool:
    """
    Validate that a Decimal doesn't exceed precision limits.
    Returns False if value would lose precision in EVM calculations.
    """
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int):
        return abs(exponent) <= max_decimal_places
    return True


def to_timeout_float(value: Union[Decimal, float, int]) -> float:
    """Convert to float for asyncio timeouts (non-financial operation)."""
    if isinstance(value, Decimal):
        return value.__float__()
    return float(value)


def format_for_api(value: Decimal) -> str:
    """Convert Decimal to plain string without scientific notation."""
    rendered = f"{safe_decimal(value):.18f}"
    return rendered.rstrip("0").rstrip(".")
