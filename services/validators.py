"""Boundary input validators for trading operations."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
import re


class BoundaryValidator:
    """Validate and normalize inputs at system boundaries."""

    PRICE_QUANT = Decimal("0.0001")
    QUANTITY_QUANT = Decimal("0.01")
    MIN_PRICE = Decimal("0.01")
    MAX_PRICE = Decimal("0.99")
    MIN_QUANTITY = Decimal("0")
    MAX_QUANTITY = Decimal("100000")

    # Polymarket uses two valid market_id formats:
    #   1. Condition ID  — 0x-prefixed 64-hex-char string (CLOB / MATIC format)
    #   2. Integer ID    — plain decimal number string   (REST API / gamma format)
    # Both must be accepted; rejecting numeric IDs blocks all paper trades where
    # the strategy engine returns REST-API-style integer market IDs.
    MARKET_ID_PATTERN = re.compile(r"^(0x[a-fA-F0-9]{64}|[0-9]+)$")

    @classmethod
    def _to_decimal(cls, value: Any, field_name: str) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            raise ValueError(f"Invalid {field_name}: float not allowed")
        if isinstance(value, (int, str)):
            try:
                dec = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Invalid {field_name}: {value}") from exc
            if dec.is_nan() or dec.is_infinite():
                raise ValueError(f"Invalid {field_name}: {value}")
            return dec
        raise ValueError(f"Invalid {field_name}: {value}")

    @classmethod
    def validate_price(cls, price: Any) -> Decimal:
        dec = cls._to_decimal(price, "price").quantize(cls.PRICE_QUANT, rounding=ROUND_HALF_UP)
        if not (cls.MIN_PRICE <= dec <= cls.MAX_PRICE):
            raise ValueError(f"Invalid price: {dec}")
        return dec

    @classmethod
    def validate_quantity(cls, quantity: Any) -> Decimal:
        dec = cls._to_decimal(quantity, "quantity").quantize(cls.QUANTITY_QUANT, rounding=ROUND_HALF_UP)
        if dec <= cls.MIN_QUANTITY:
            raise ValueError(f"Invalid quantity: {dec}")
        if dec > cls.MAX_QUANTITY:
            raise ValueError(f"Invalid quantity: {dec}")
        return dec

    @classmethod
    def validate_market_id(cls, market_id: Any) -> str:
        if not isinstance(market_id, str):
            raise ValueError(f"Invalid market_id: expected str, got {type(market_id).__name__!r} value={market_id!r}")
        market_id = market_id.strip()
        if not cls.MARKET_ID_PATTERN.match(market_id):
            raise ValueError(
                f"Invalid market_id: {market_id!r} does not match expected formats "
                f"(0x<64hex> or decimal integer)"
            )
        return market_id

    @classmethod
    def validate_side(cls, side: Any) -> str:
        if hasattr(side, "value"):
            side = side.value
        if not isinstance(side, str):
            raise ValueError("Invalid side")
        side = side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("Invalid side")
        return side
