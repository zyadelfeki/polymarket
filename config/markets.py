"""
Market Configuration
Defines target markets and trading parameters
"""
from typing import Dict, List

class MarketConfig:
    """Trading market definitions"""
    
    # Supported cryptocurrencies
    SYMBOLS = ["BTC", "ETH", "SOL"]
    
    # Binance trading pairs
    BINANCE_PAIRS = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT"
    }
    
    # Polymarket category filters
    POLYMARKET_CATEGORIES = [
        "crypto",
        "bitcoin",
        "ethereum"
    ]
    
    # Market resolution time preferences (hours)
    PREFERRED_RESOLUTION_MAX_HOURS = 24
    FAST_RESOLUTION_MAX_HOURS = 6
    
    # Minimum liquidity thresholds
    MIN_MARKET_LIQUIDITY = 100  # USD
    IDEAL_MARKET_LIQUIDITY = 1000  # USD
    
    # Keywords for high-priority markets
    HIGH_PRIORITY_KEYWORDS = [
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "price",
        "above",
        "below",
        "today",
        "tomorrow"
    ]
    
    @classmethod
    def is_high_priority_market(cls, title: str) -> bool:
        """Check if market matches high-priority criteria"""
        title_lower = title.lower()
        return any(keyword in title_lower for keyword in cls.HIGH_PRIORITY_KEYWORDS)

market_config = MarketConfig()