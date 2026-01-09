from datetime import datetime, timedelta
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, bankroll_tracker):
        self.bankroll = bankroll_tracker
        self.enabled = settings.CIRCUIT_BREAKER_ENABLED
        self.tripped = False
        self.trip_reason = None
        self.trip_time = None
        self.daily_start_capital = self.bankroll.get_current_capital()
        self.daily_reset_time = datetime.utcnow()
    
    def check_should_trade(self) -> tuple[bool, Optional[str]]:
        if not self.enabled:
            return True, None
        
        if self.tripped:
            return False, self.trip_reason
        
        if (datetime.utcnow() - self.daily_reset_time) > timedelta(hours=24):
            self.daily_start_capital = self.bankroll.get_current_capital()
            self.daily_reset_time = datetime.utcnow()
        
        current_capital = self.bankroll.get_current_capital()
        initial_capital = float(settings.INITIAL_CAPITAL)
        
        drawdown_pct = ((initial_capital - current_capital) / initial_capital) * 100
        if drawdown_pct >= settings.MAX_DRAWDOWN_PCT:
            self.trip(f"Max drawdown reached: {drawdown_pct:.1f}%")
            return False, self.trip_reason
        
        daily_loss_pct = ((self.daily_start_capital - current_capital) / self.daily_start_capital) * 100
        if daily_loss_pct >= settings.DAILY_LOSS_LIMIT_PCT:
            self.trip(f"Daily loss limit: {daily_loss_pct:.1f}%")
            return False, self.trip_reason
        
        streak = self.bankroll.get_consecutive_streak()
        if streak['losses'] >= settings.MAX_CONSECUTIVE_LOSSES:
            self.trip(f"Max consecutive losses: {streak['losses']}")
            return False, self.trip_reason
        
        return True, None
    
    def trip(self, reason: str):
        self.tripped = True
        self.trip_reason = reason
        self.trip_time = datetime.utcnow()
        logger.error(f"🚨 CIRCUIT BREAKER TRIPPED: {reason}")
        logger.error("Trading HALTED. Manual review required.")
    
    def reset(self):
        self.tripped = False
        self.trip_reason = None
        self.trip_time = None
        logger.info("✅ Circuit breaker RESET")
    
    def get_status(self) -> Dict:
        return {
            "enabled": self.enabled,
            "tripped": self.tripped,
            "reason": self.trip_reason,
            "trip_time": self.trip_time.isoformat() if self.trip_time else None
        }