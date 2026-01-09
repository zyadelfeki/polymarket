"""
Adaptive Kelly Criterion Position Sizing
Dynamically adjusts bet sizes based on recent performance
"""
from typing import Dict, Optional
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

class AdaptiveKellySizer:
    """Dynamic position sizing with Kelly Criterion"""
    
    def __init__(self, initial_capital: Decimal, max_position_pct: float = 20.0):
        self.initial_capital = initial_capital
        self.max_position_pct = max_position_pct
        
        # Performance tracking
        self.recent_trades = []
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        
        # Kelly multiplier adjustments
        self.base_kelly_fraction = 0.5  # Use half Kelly for safety
        self.current_kelly_multiplier = 1.0
    
    def calculate_bet_size(self, bankroll: Decimal, win_probability: float, 
                          payout_odds: float, confidence: float) -> Decimal:
        """
        Calculate optimal bet size using adaptive Kelly
        
        Args:
            bankroll: Current available capital
            win_probability: Estimated probability of winning (0-1)
            payout_odds: Decimal odds (e.g., 2.0 = 2x payout)
            confidence: Signal confidence (0-1)
        
        Returns:
            Bet size in dollars
        """
        # Kelly formula: f = (bp - q) / b
        # f = fraction to bet
        # b = odds - 1
        # p = win probability
        # q = loss probability (1-p)
        
        b = payout_odds - 1
        p = win_probability
        q = 1 - p
        
        if b <= 0:
            return Decimal("0")
        
        # Base Kelly fraction
        kelly_fraction = (b * p - q) / b
        
        # Apply safety factor (half Kelly)
        kelly_fraction *= self.base_kelly_fraction
        
        # Apply adaptive multiplier based on streak
        kelly_fraction *= self.current_kelly_multiplier
        
        # Adjust by confidence
        kelly_fraction *= confidence
        
        # Ensure positive and within limits
        kelly_fraction = max(0.0, min(kelly_fraction, self.max_position_pct / 100))
        
        # Calculate bet size
        bet_size = bankroll * Decimal(str(kelly_fraction))
        
        # Minimum bet
        bet_size = max(bet_size, Decimal("0.50"))
        
        # Maximum bet (20% of bankroll)
        max_bet = bankroll * Decimal(str(self.max_position_pct / 100))
        bet_size = min(bet_size, max_bet)
        
        return bet_size
    
    def record_trade_result(self, won: bool, profit: Decimal):
        """Record trade outcome and update multiplier"""
        self.recent_trades.append({
            "won": won,
            "profit": profit
        })
        
        # Keep only last 20 trades
        if len(self.recent_trades) > 20:
            self.recent_trades.pop(0)
        
        # Update streak counters
        if won:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        # Adjust Kelly multiplier based on performance
        self._adjust_kelly_multiplier()
    
    def _adjust_kelly_multiplier(self):
        """
        Dynamically adjust Kelly multiplier
        
        Logic:
        - Winning streak + low volatility = increase aggression
        - Losing streak = reduce risk
        - Win rate above 60% = increase
        - Win rate below 50% = decrease
        """
        # Calculate recent win rate
        if len(self.recent_trades) >= 5:
            recent_wins = sum(1 for t in self.recent_trades[-10:] if t["won"])
            win_rate = recent_wins / min(len(self.recent_trades), 10)
        else:
            win_rate = 0.5  # Default neutral
        
        # Base multiplier
        multiplier = 1.0
        
        # Streak adjustments
        if self.consecutive_wins >= 3:
            multiplier *= 1.3  # Increase by 30%
            logger.info(f"⬆️ Kelly multiplier UP: {multiplier:.2f}x (win streak)")
        elif self.consecutive_losses >= 2:
            multiplier *= 0.5  # Decrease by 50%
            logger.warning(f"⬇️ Kelly multiplier DOWN: {multiplier:.2f}x (loss streak)")
        
        # Win rate adjustments
        if win_rate >= 0.65:
            multiplier *= 1.2  # Strong performance
        elif win_rate <= 0.45:
            multiplier *= 0.7  # Poor performance
        
        # Clamp to reasonable range
        multiplier = max(0.3, min(multiplier, 1.5))
        
        self.current_kelly_multiplier = multiplier
    
    def get_stats(self) -> Dict:
        """Return sizing statistics"""
        if not self.recent_trades:
            return {
                "kelly_multiplier": self.current_kelly_multiplier,
                "consecutive_wins": self.consecutive_wins,
                "consecutive_losses": self.consecutive_losses,
                "win_rate": 0.0
            }
        
        recent_wins = sum(1 for t in self.recent_trades if t["won"])
        win_rate = recent_wins / len(self.recent_trades)
        
        return {
            "kelly_multiplier": self.current_kelly_multiplier,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "win_rate": win_rate,
            "total_trades": len(self.recent_trades)
        }