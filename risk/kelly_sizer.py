from decimal import Decimal
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class AdaptiveKellySizer:
    def __init__(self):
        self.base_kelly_fraction = 0.5
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.recent_trades = []
        self.volatility_multiplier = 1.0
    
    def calculate_bet_size(
        self,
        bankroll: Decimal,
        win_probability: float,
        payout_odds: float,
        edge: float,
        volatility_regime: str = "normal"
    ) -> Decimal:
        
        if win_probability <= 0 or win_probability >= 1:
            return Decimal("0")
        
        if payout_odds <= 1:
            return Decimal("0")
        
        b = payout_odds - 1
        p = win_probability
        q = 1 - p
        
        kelly_fraction = (b * p - q) / b
        
        if kelly_fraction <= 0:
            return Decimal("0")
        
        adjusted_kelly = self._adjust_for_streak(kelly_fraction)
        adjusted_kelly = self._adjust_for_volatility(adjusted_kelly, volatility_regime)
        adjusted_kelly = self._adjust_for_edge(adjusted_kelly, edge)
        
        bet_size = bankroll * Decimal(str(adjusted_kelly)) * Decimal(str(self.base_kelly_fraction))
        
        max_bet = bankroll * Decimal(str(settings.MAX_POSITION_SIZE_PCT / 100))
        bet_size = min(bet_size, max_bet)
        
        reserve = bankroll * Decimal(str(settings.CASH_RESERVE_PCT / 100))
        available = bankroll - reserve
        bet_size = min(bet_size, available)
        
        if bet_size < settings.MIN_BET_SIZE:
            return Decimal("0")
        
        return bet_size.quantize(Decimal("0.01"))
    
    def _adjust_for_streak(self, kelly_fraction: float) -> float:
        if self.consecutive_wins >= 3:
            multiplier = 1.2
            logger.debug(f"Win streak bonus: {multiplier}x Kelly")
            return kelly_fraction * multiplier
        elif self.consecutive_losses >= 2:
            multiplier = 0.5
            logger.debug(f"Loss streak reduction: {multiplier}x Kelly")
            return kelly_fraction * multiplier
        return kelly_fraction
    
    def _adjust_for_volatility(self, kelly_fraction: float, regime: str) -> float:
        if regime == "high":
            return kelly_fraction * 0.7
        elif regime == "extreme":
            return kelly_fraction * 0.5
        return kelly_fraction
    
    def _adjust_for_edge(self, kelly_fraction: float, edge: float) -> float:
        if edge > 0.30:
            return kelly_fraction * 1.3
        elif edge > 0.20:
            return kelly_fraction * 1.1
        elif edge < 0.10:
            return kelly_fraction * 0.8
        return kelly_fraction
    
    def record_trade_result(self, win: bool, profit: float):
        self.recent_trades.append({"win": win, "profit": profit})
        if len(self.recent_trades) > 20:
            self.recent_trades.pop(0)
        
        if win:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
    
    def get_win_rate(self) -> float:
        if not self.recent_trades:
            return 0.5
        wins = sum(1 for t in self.recent_trades if t["win"])
        return wins / len(self.recent_trades)
    
    def reset_streak(self):
        self.consecutive_wins = 0
        self.consecutive_losses = 0