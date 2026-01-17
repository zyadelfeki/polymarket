"""Compatibility shim for imports at repository root."""

from data_feeds.polymarket_client_v2 import *  # noqa: F403
from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide  # noqa: F401

__all__ = ["PolymarketClientV2", "OrderSide"]
