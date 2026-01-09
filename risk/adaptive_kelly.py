from typing import Dict, Optional
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class AdaptiveKellySizer:
    def __init__(self, bankroll_tracker):
        self.bankroll = bankroll_tracker
        self.base_kelly_fraction = 0.25
        self.conservative_mode = False
    
    def calculate_bet_size(self, win_probability: float, payout_odds: float, edge: float, strategy: str = "default") -> float:
        available_capital = self.bankroll.get_available_capital()
        if available_capital <= 0:
            return 0.0
        
        b = payout_odds - 1
        p = win_probability
        q = 1 - p
        
        if b <= 0:
            return 0.0
        
        kelly_fraction = (b * p - q) / b
        if kelly_fraction <= 0:
            return 0.0
        
        streak = self.bankroll.get_consecutive_streak()
        volatility_regime = "low"
        
        multiplier = 1.0
        if streak['wins'] >= 3 and volatility_regime == "low":
            multiplier = 1.5
            logger.info("⬆️  Kelly increased: win streak detected")
        elif streak['losses'] >= 2:
            multiplier = 0.3
            logger.warning("⬇️  Kelly reduced: loss streak protection")
        elif self.conservative_mode:
            multiplier = 0.5
        
        adjusted_fraction = self.base_kelly_fraction * kelly_fraction * multiplier
        bet_size = available_capital * adjusted_fraction
        
        max_position = available_capital * (settings.MAX_POSITION_SIZE_PCT / 100)
        bet_size = min(bet_size, max_position)
        bet_size = max(bet_size, float(settings.MIN_BET_SIZE))
        
        if edge > 0.30:
            bet_size = min(bet_size * 1.3, max_position)
        
        logger.debug(f"Kelly calc: p={p:.2f}, odds={payout_odds:.2f}, fraction={kelly_fraction:.3f}, bet=${bet_size:.2f}")
        return bet_size
    
    def enable_conservative_mode(self):
        self.conservative_mode = True
        logger.warning("🛡️  Conservative mode ENABLED")
    
    def disable_conservative_mode(self):
        self.conservative_mode = False
        logger.info("✅ Conservative mode disabled")