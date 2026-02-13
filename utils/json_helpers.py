"""
Fast JSON serialization using orjson.
Handles Decimal types automatically.
"""
from decimal import Decimal
from typing import Any

import orjson


def dumps(obj: Any) -> str:
    """Serialize to JSON string (Decimal-safe)."""
    return orjson.dumps(
        obj,
        option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_APPEND_NEWLINE,
        default=_decimal_serializer,
    ).decode("utf-8")


def loads(data: str | bytes) -> Any:
    """Deserialize from JSON string."""
    return orjson.loads(data)


def _decimal_serializer(obj: Any):
    """Custom serializer for Decimal types."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
