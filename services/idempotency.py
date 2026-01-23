"""Idempotency utilities for order deduplication."""

import time
import uuid
from decimal import Decimal
from typing import Dict, Optional, Tuple, Any


class IdempotencyKeyBuilder:
    """Deterministic idempotency key from order parameters."""

    @staticmethod
    def build(
        strategy: str,
        market_id: str,
        token_id: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        order_type: str = "GTC",
        override_key: Optional[str] = None,
    ) -> str:
        if override_key:
            return override_key

        key_material = f"{strategy}:{market_id}:{token_id}:{side}:{order_type}:{quantity}:{price}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, key_material))


class IdempotencyCache:
    """Minimal in-memory TTL cache for order deduplication."""

    def __init__(self, ttl_seconds: Optional[int] = 86400):
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None

        result, timestamp = self._cache[key]
        if self.ttl_seconds is not None:
            age = time.time() - timestamp
            if age > self.ttl_seconds:
                del self._cache[key]
                return None

        return result

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = (value, time.time())

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)
