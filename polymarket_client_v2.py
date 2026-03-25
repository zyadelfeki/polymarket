"""Compatibility shim — legacy import alias for root-level imports.

Do NOT add logic here. Import directly from data_feeds.polymarket_client_v2.
This file exists only so that code written before the data_feeds/ refactor
continues to work without modification.
"""

from data_feeds.polymarket_client_v2 import *  # noqa: F403
from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide  # noqa: F401

__all__ = ["PolymarketClientV2", "OrderSide"]
