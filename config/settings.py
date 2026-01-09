import os
from typing import Optional
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

class Settings:
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
    TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_SECRET: str = os.getenv("POLYMARKET_SECRET", "")
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    
    INITIAL_CAPITAL: Decimal = Decimal(os.getenv("INITIAL_CAPITAL", "15.00"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "20"))
    MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "15"))
    MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.15"))
    MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.70"))
    
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    MIN_BET_SIZE: Decimal = Decimal("0.50")
    CASH_RESERVE_PCT: float = 50.0
    
    CIRCUIT_BREAKER_ENABLED: bool = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
    MAX_CONSECUTIVE_LOSSES: int = 3
    DAILY_LOSS_LIMIT_PCT: float = 10.0
    
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    
    NEWS_SCAN_INTERVAL: int = 10
    PRICE_CHECK_INTERVAL: int = 1
    TWITTER_SCAN_INTERVAL: int = 60
    MARKET_DISCOVERY_INTERVAL: int = 300
    
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./data/trades.db")
    
    @classmethod
    def validate(cls) -> bool:
        if not cls.POLYMARKET_API_KEY:
            print("ERROR: POLYMARKET_API_KEY required")
            return False
        return True

settings = Settings()