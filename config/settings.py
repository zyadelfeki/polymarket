"""
Configuration Management
Type-safe environment variable loading with validation
"""
import os
from typing import Optional
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

class Settings:
    """Centralized configuration with validation"""
    
    # API Keys
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
    TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    
    # Capital Management
    INITIAL_CAPITAL: Decimal = Decimal(os.getenv("INITIAL_CAPITAL", "15.00"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "20"))
    MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "15"))
    MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.15"))
    MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.70"))
    
    # Position Limits
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    MIN_BET_SIZE: Decimal = Decimal("0.50")
    CASH_RESERVE_PCT: float = 50.0
    
    # Risk Management
    CIRCUIT_BREAKER_ENABLED: bool = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
    MAX_CONSECUTIVE_LOSSES: int = 3
    DAILY_LOSS_LIMIT_PCT: float = 10.0
    
    # Volatility Arbitrage Settings
    VOLATILITY_SPIKE_THRESHOLD: float = 3.0  # Percent move in 60 seconds
    PANIC_BUY_MAX_PRICE: float = 0.10  # Buy positions under $0.10
    PANIC_SELL_TARGET: float = 0.40  # Sell at $0.40+ (4x minimum)
    MAX_PANIC_POSITIONS: int = 2
    
    # Trading Mode
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
    
    # Data Refresh Intervals (seconds)
    PRICE_UPDATE_INTERVAL: float = 0.1  # 100ms for WebSocket
    NEWS_SCAN_INTERVAL: int = 10
    MARKET_SCAN_INTERVAL: int = 5
    POSITION_CHECK_INTERVAL: int = 30
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "./data/trades.db")
    
    @classmethod
    def validate(cls) -> bool:
        """Validate critical settings"""
        if cls.PAPER_TRADING:
            return True  # Allow paper trading without keys
            
        required = [
            ("POLYMARKET_PRIVATE_KEY", cls.POLYMARKET_PRIVATE_KEY),
        ]
        
        missing = [name for name, value in required if not value]
        
        if missing:
            print(f"❌ Missing required: {', '.join(missing)}")
            return False
            
        return True
    
    @classmethod
    def print_config(cls):
        """Display configuration"""
        print("\n" + "="*60)
        print("⚡ POLYMARKET VOLATILITY ARBITRAGE BOT V2.0")
        print("="*60)
        print(f"💰 Capital: ${cls.INITIAL_CAPITAL}")
        print(f"📊 Max Position: {cls.MAX_POSITION_SIZE_PCT}%")
        print(f"🛡️  Max Drawdown: {cls.MAX_DRAWDOWN_PCT}%")
        print(f"⚠️  Paper Mode: {'YES' if cls.PAPER_TRADING else 'LIVE'}")
        print(f"🚨 Volatility Threshold: {cls.VOLATILITY_SPIKE_THRESHOLD}%")
        print("="*60 + "\n")

settings = Settings()