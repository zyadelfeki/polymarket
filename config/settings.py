import os
from typing import Optional
from dotenv import load_dotenv
from decimal import Decimal
import logging

load_dotenv()

logger = logging.getLogger(__name__)

class Settings:
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
    TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")
    POLYMARKET_SECRET: str = os.getenv("POLYMARKET_SECRET", "")
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")
    
    INITIAL_CAPITAL: Decimal = Decimal(os.getenv("INITIAL_CAPITAL", "15.00"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "20"))
    MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "15"))
    MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.15"))
    MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.70"))
    
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    MIN_BET_SIZE: Decimal = Decimal("0.10")
    CASH_RESERVE_PCT: float = 50.0
    
    CIRCUIT_BREAKER_ENABLED: bool = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
    MAX_CONSECUTIVE_LOSSES: int = 3
    DAILY_LOSS_LIMIT_PCT: float = 10.0
    MAX_DAILY_TRADES: int = int(os.getenv("MAX_DAILY_TRADES", "50"))
    
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    
    VOLATILITY_STRATEGY_WEIGHT: float = float(os.getenv("VOLATILITY_STRATEGY_WEIGHT", "0.40"))
    WHALE_COPY_WEIGHT: float = float(os.getenv("WHALE_COPY_WEIGHT", "0.30"))
    NEWS_ARBITRAGE_WEIGHT: float = float(os.getenv("NEWS_ARBITRAGE_WEIGHT", "0.20"))
    BOND_STRATEGY_WEIGHT: float = float(os.getenv("BOND_STRATEGY_WEIGHT", "0.10"))
    
    MAX_SLIPPAGE_PCT: float = float(os.getenv("MAX_SLIPPAGE_PCT", "5"))
    ORDER_SPLIT_THRESHOLD: Decimal = Decimal(os.getenv("ORDER_SPLIT_THRESHOLD", "5.00"))
    EXECUTION_TIMEOUT_SEC: int = int(os.getenv("EXECUTION_TIMEOUT_SEC", "10"))
    
    NEWS_SCAN_INTERVAL: int = 10
    PRICE_CHECK_INTERVAL: int = 1
    TWITTER_SCAN_INTERVAL: int = 60
    MARKET_DISCOVERY_INTERVAL: int = 300
    WHALE_TRACK_INTERVAL: int = 30
    
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./data/trades.db")
    ENABLE_PERFORMANCE_LOGGING: bool = os.getenv("ENABLE_PERFORMANCE_LOGGING", "true").lower() == "true"
    
    VOLATILITY_SPIKE_THRESHOLD: float = 3.0
    EXTREME_DISCOUNT_THRESHOLD: float = 0.05
    GOOD_DISCOUNT_THRESHOLD: float = 0.10
    PROFIT_TARGET_MIN: float = 0.30
    PROFIT_TARGET_IDEAL: float = 0.60
    
    WHALE_MIN_BET_SIZE: Decimal = Decimal("1000.00")
    WHALE_COPY_RATIO: float = 0.0002
    
    FAST_RESOLUTION_MAX_HOURS: int = 24
    MIN_MARKET_LIQUIDITY: Decimal = Decimal("100.00")
    
    @classmethod
    def validate(cls) -> bool:
        critical = [
            ("POLYMARKET_PRIVATE_KEY", cls.POLYMARKET_PRIVATE_KEY),
        ]
        
        missing = [name for name, value in critical if not value]
        
        if missing:
            logger.error(f"Missing critical config: {', '.join(missing)}")
            return False
            
        return True
    
    @classmethod
    def log_config(cls):
        logger.info("="*60)
        logger.info("POLYMARKET BOT V2 - PRODUCTION CONFIG")
        logger.info("="*60)
        logger.info(f"Capital: ${cls.INITIAL_CAPITAL}")
        logger.info(f"Paper Trading: {cls.PAPER_TRADING}")
        logger.info(f"Max Position: {cls.MAX_POSITION_SIZE_PCT}%")
        logger.info(f"Max Drawdown: {cls.MAX_DRAWDOWN_PCT}%")
        logger.info(f"Strategy Weights: VOL={cls.VOLATILITY_STRATEGY_WEIGHT} WHALE={cls.WHALE_COPY_WEIGHT} NEWS={cls.NEWS_ARBITRAGE_WEIGHT} BOND={cls.BOND_STRATEGY_WEIGHT}")
        logger.info("="*60)

settings = Settings()