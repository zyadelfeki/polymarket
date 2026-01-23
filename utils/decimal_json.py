"""Decimal-safe JSON serialization utilities."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class DecimalJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any):
        if isinstance(obj, Decimal):
            return {"__decimal__": str(obj)}
        return super().default(obj)


def _decimal_object_hook(obj: dict):
    if "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    return obj


def dumps(payload: Any, **kwargs) -> str:
    return json.dumps(payload, cls=DecimalJSONEncoder, **kwargs)


def loads(payload: str, **kwargs) -> Any:
    return json.loads(payload, object_hook=_decimal_object_hook, **kwargs)
