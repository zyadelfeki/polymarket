#!/usr/bin/env python3
"""
Production Kelly Criterion Position Sizer

Implements fractional Kelly with strict safety controls.

Key principles:
1. Never use full Kelly - always fractional (1/4 to 1/2 Kelly)
2. Cap maximum bet size regardless of Kelly calculation
3. Require minimum sample size before trusting model probabilities
4. Reduce sizing during losing streaks and volatility spikes
5. Never bet with zero or negative edge

References:
- Thorp, E. O. (2006). "The Kelly Criterion in Blackjack Sports Betting, and the Stock Market"
- MacLean, Thorp, Ziemba (2011). "The Kelly Capital Growth Investment Criterion"
"""

from decimal import Decimal, ROUND_DOWN
import logging
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class BetSizeResult:
    """Result of bet sizing calculation"""
    size: Decimal
    kelly_fraction: float
    effective_fraction: float
    capped_reason: Optional[str] = None
    warnings: list = None

class AdaptiveKellySizer:
    """
    Production-grade Kelly Criterion position sizer.
    
    Safety features:
    - Fractional Kelly (default 1/4)
    - Absolute cap per trade (default 5% of bankroll)
    - Minimum edge threshold (2%)
    - Sample size requirement for model-based probabilities
    - Streak-based reduction (cut sizing after losses)
    - Aggregate exposure limits
    """
    
    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        
        # Core Kelly parameters
        self.kelly_fraction = config.get('kelly_fraction', 0.25)  # 1/4 Kelly default
        self.max_kelly_fraction = config.get('max_kelly_fraction', 0.25)  # Hard cap
        
        # Safety limits
        self.max_bet_pct = config.get('max_bet_pct', 5.0)  # Max 5% per trade
        self.min_bet_size = Decimal(str(config.get('min_bet_size', 5.0)))  # Min $5
        self.min_edge = config.get('min_edge', 0.02)  # Minimum 2% edge
        self.min_sample_size = config.get('min_sample_size', 20)  # Need 20+ samples for model
        
        # Aggregate exposure
        self.max_aggregate_exposure_pct = config.get('max_aggregate_exposure', 20.0)  # Max 20% total
        
        # Adaptive adjustments
        self.streak_reduction_threshold = 3  # Reduce after 3 losses
        self.streak_reduction_factor = 0.5  # Cut to 50% on loss streak
        self.streak_bonus_threshold = 5  # Bonus after 5 wins
        self.streak_bonus_factor = 1.2  # 20% bonus on win streak (capped by max)
        
        # State tracking
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.recent_trades = []  # Last N trades for win rate calc
        self.max_recent_trades = 50
        
        logger.info(
            f"Kelly Sizer initialized: fraction={self.kelly_fraction}, "
            f"max_bet={self.max_bet_pct}%, min_edge={self.min_edge*100}%"
        )
    
    def calculate_bet_size(
        self,
        bankroll: Decimal,
        win_probability: float,
        payout_odds: float,
        edge: float,
        sample_size: int = 0,
        current_aggregate_exposure: Decimal = Decimal('0')
    ) -> BetSizeResult:
        """
        Calculate optimal bet size using Kelly Criterion.
        
        Args:
            bankroll: Current equity (from ledger.get_equity())
            win_probability: Estimated probability of winning (0-1)
            payout_odds: Decimal payout odds (e.g., 2.0 = double money)
            edge: Estimated edge (expected_value / bet_size)
            sample_size: Number of historical samples model is based on
            current_aggregate_exposure: Sum of open position values
        
        Returns:
            BetSizeResult with calculated size and metadata
        """
        warnings = []
        
        # Validation
        if bankroll <= 0:
            logger.warning(f"Invalid bankroll: {bankroll}")
            return BetSizeResult(Decimal('0'), 0.0, 0.0, "invalid_bankroll", warnings)
        
        if not (0 < win_probability < 1):
            logger.warning(f"Invalid win probability: {win_probability}")
            return BetSizeResult(Decimal('0'), 0.0, 0.0, "invalid_probability", warnings)
        
        if payout_odds <= 1.0:
            logger.warning(f"Invalid payout odds: {payout_odds}")
            return BetSizeResult(Decimal('0'), 0.0, 0.0, "invalid_odds", warnings)
        
        # Check minimum edge
        if edge < self.min_edge:
            logger.debug(f"Edge too small: {edge:.2%} < {self.min_edge:.2%}")
            return BetSizeResult(Decimal('0'), 0.0, 0.0, "insufficient_edge", warnings)
        
        # Check sample size for model-based probabilities
        if sample_size > 0 and sample_size < self.min_sample_size:
            warnings.append(f"Low sample size: {sample_size} < {self.min_sample_size}")
            # Reduce Kelly fraction for low-confidence estimates
            effective_kelly = self.kelly_fraction * 0.5
            logger.debug(f"Reducing Kelly to {effective_kelly:.2%} due to low sample size")
        else:
            effective_kelly = self.kelly_fraction
        
        # Calculate Kelly fraction
        # Kelly formula: f = (bp - q) / b
        # where b = payout_odds - 1, p = win_prob, q = 1 - p
        b = payout_odds - 1.0
        p = win_probability
        q = 1.0 - p
        
        kelly_f = (b * p - q) / b
        
        # Kelly can be negative (bad bet) or > 1 (over-leveraged)
        if kelly_f <= 0:
            logger.debug(f"Negative Kelly: {kelly_f:.4f}")
            return BetSizeResult(Decimal('0'), kelly_f, 0.0, "negative_kelly", warnings)
        
        # Cap Kelly fraction
        kelly_f = min(kelly_f, self.max_kelly_fraction)
        
        # Apply fractional Kelly
        bet_fraction = kelly_f * effective_kelly
        
        # Streak adjustments
        if self.consecutive_losses >= self.streak_reduction_threshold:
            bet_fraction *= self.streak_reduction_factor
            warnings.append(f"Loss streak reduction: {self.consecutive_losses} losses")
            logger.debug(f"Reducing bet size due to {self.consecutive_losses} consecutive losses")
        
        elif self.consecutive_wins >= self.streak_bonus_threshold:
            # Small bonus for win streaks, but still capped
            bet_fraction = min(bet_fraction * self.streak_bonus_factor, self.max_kelly_fraction)
            logger.debug(f"Win streak bonus: {self.consecutive_wins} wins")
        
        # Calculate dollar size
        bet_size = bankroll * Decimal(str(bet_fraction))
        
        # Apply maximum bet size cap
        max_bet = bankroll * Decimal(str(self.max_bet_pct / 100.0))
        if bet_size > max_bet:
            bet_size = max_bet
            warnings.append(f"Capped at max bet: {self.max_bet_pct}%")
        
        # Check aggregate exposure limit
        if current_aggregate_exposure > 0:
            max_aggregate = bankroll * Decimal(str(self.max_aggregate_exposure_pct / 100.0))
            available_exposure = max_aggregate - current_aggregate_exposure
            
            if available_exposure <= 0:
                logger.warning(
                    f"Aggregate exposure limit reached: {current_aggregate_exposure} / {max_aggregate}"
                )
                return BetSizeResult(
                    Decimal('0'), kelly_f, bet_fraction,
                    "aggregate_exposure_limit", warnings
                )
            
            if bet_size > available_exposure:
                bet_size = available_exposure
                warnings.append("Capped by aggregate exposure limit")
        
        # Apply minimum bet size
        if bet_size < self.min_bet_size:
            logger.debug(f"Bet size {bet_size} below minimum {self.min_bet_size}")
            return BetSizeResult(
                Decimal('0'), kelly_f, bet_fraction,
                "below_minimum", warnings
            )
        
        # Round down to 2 decimals (safer than rounding up)
        bet_size = bet_size.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        
        logger.info(
            f"Kelly bet size: ${bet_size} ({bet_fraction:.2%} of ${bankroll}) | "
            f"Edge: {edge:.2%} | Win prob: {win_probability:.2%} | "
            f"Kelly_f: {kelly_f:.4f}"
        )
        
        return BetSizeResult(
            size=bet_size,
            kelly_fraction=kelly_f,
            effective_fraction=bet_fraction,
            warnings=warnings if warnings else None
        )
    
    def record_trade_result(
        self,
        win: bool,
        roi: float,
        bet_size: float,
        strategy: str
    ):
        """
        Record trade result for streak tracking and win rate estimation.
        
        Args:
            win: True if trade was profitable
            roi: Return on investment (profit / bet_size)
            bet_size: Size of bet in USDC
            strategy: Strategy name
        """
        trade = {
            'win': win,
            'roi': roi,
            'bet_size': bet_size,
            'strategy': strategy
        }
        
        self.recent_trades.append(trade)
        if len(self.recent_trades) > self.max_recent_trades:
            self.recent_trades.pop(0)
        
        # Update streak counters
        if win:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        logger.debug(
            f"Trade recorded: {'WIN' if win else 'LOSS'} | ROI: {roi:+.2%} | "
            f"Streak: {self.consecutive_wins}W / {self.consecutive_losses}L"
        )
    
    def get_win_rate(self, strategy: Optional[str] = None, min_samples: int = 10) -> Optional[float]:
        """
        Calculate win rate from recent trades.
        
        Args:
            strategy: Filter by strategy (None = all)
            min_samples: Minimum trades required to return a rate
        
        Returns:
            Win rate (0-1) or None if insufficient data
        """
        trades = self.recent_trades
        if strategy:
            trades = [t for t in trades if t['strategy'] == strategy]
        
        if len(trades) < min_samples:
            return None
        
        wins = sum(1 for t in trades if t['win'])
        return wins / len(trades)
    
    def reset_streaks(self):
        """Reset win/loss streaks (e.g., after circuit breaker)"""
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        logger.info("Streaks reset")
    
    def get_stats(self) -> dict:
        """Get current sizing statistics"""
        return {
            'kelly_fraction': self.kelly_fraction,
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses,
            'recent_trade_count': len(self.recent_trades),
            'win_rate': self.get_win_rate() if len(self.recent_trades) >= 10 else None
        }