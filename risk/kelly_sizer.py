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

from decimal import Decimal, ROUND_DOWN, getcontext
import logging
from typing import Optional
from dataclasses import dataclass
from utils.decimal_helpers import to_decimal, quantize_quantity

logger = logging.getLogger(__name__)

getcontext().prec = 18

@dataclass
class BetSizeResult:
    """Result of bet sizing calculation"""
    size: Decimal
    kelly_fraction: Decimal
    effective_fraction: Decimal
    capped_reason: Optional[str] = None
    warnings: list = None

    def __float__(self) -> float:
        raise TypeError("Float arithmetic forbidden in financial calculations")

    def _coerce_other(self, other):
        if isinstance(other, BetSizeResult):
            return other.size
        return Decimal(str(other))

    def __lt__(self, other):
        return self.size < self._coerce_other(other)

    def __le__(self, other):
        return self.size <= self._coerce_other(other)

    def __gt__(self, other):
        return self.size > self._coerce_other(other)

    def __ge__(self, other):
        return self.size >= self._coerce_other(other)

    def __eq__(self, other):
        return self.size == self._coerce_other(other)

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
        self.kelly_fraction = Decimal(str(config.get('kelly_fraction', "0.25")))
        self.max_kelly_fraction = Decimal(str(config.get('max_kelly_fraction', "0.25")))
        
        # Safety limits
        self.max_bet_pct = Decimal(str(config.get('max_bet_pct', "5.0")))
        self.min_bet_size = Decimal(str(config.get('min_bet_size', "1.0")))  # Min $1
        self.min_edge = Decimal(str(config.get('min_edge', "0.02")))
        self.min_sample_size = config.get('min_sample_size', 20)  # Need 20+ samples for model
        
        # Aggregate exposure
        self.max_aggregate_exposure_pct = Decimal(str(config.get('max_aggregate_exposure', "20.0")))
        
        # Adaptive adjustments
        self.streak_reduction_threshold = 3  # Reduce after 3 losses
        self.streak_reduction_factor = Decimal("0.5")  # Cut to 50% on loss streak
        self.streak_bonus_threshold = 5  # Bonus after 5 wins
        self.streak_bonus_factor = Decimal("1.2")  # 20% bonus on win streak (capped by max)
        
        # State tracking
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.recent_trades = []  # Last N trades for win rate calc
        self.max_recent_trades = 50
        
        logger.info(
            f"Kelly Sizer initialized: fraction={self.kelly_fraction}, "
            f"max_bet={self.max_bet_pct}%, min_edge={self.min_edge * Decimal('100')}%"
        )

    def calculate_bet_size(
        self,
        bankroll: Decimal,
        win_probability: Optional[Decimal] = None,
        payout_odds: Optional[Decimal] = None,
        edge: Optional[Decimal] = None,
        sample_size: int = 0,
        current_aggregate_exposure: Decimal = Decimal('0'),
        market_price: Optional[Decimal] = None,
        current_exposure: Optional[Decimal] = None,
    ) -> BetSizeResult:
        warnings = []

        if current_exposure is not None:
            current_aggregate_exposure = current_exposure

        if bankroll <= 0:
            logger.warning(f"Invalid bankroll: {bankroll}")
            return BetSizeResult(Decimal('0'), Decimal("0"), Decimal("0"), "invalid_bankroll", warnings)

        edge_dec = to_decimal(edge) if edge is not None else Decimal('0')

        if market_price is not None:
            payout_odds = Decimal('1') / to_decimal(market_price)
            win_probability = max(
                Decimal('0'),
                min(Decimal('1'), to_decimal(market_price) + edge_dec)
            )

        if win_probability is None or payout_odds is None:
            logger.warning("Missing win_probability or payout_odds")
            return BetSizeResult(Decimal('0'), Decimal('0'), Decimal('0'), "invalid_probability", warnings)

        win_probability = to_decimal(win_probability)
        payout_odds = to_decimal(payout_odds)

        if not (Decimal('0') < win_probability < Decimal('1')):
            logger.warning(f"Invalid win probability: {win_probability}")
            return BetSizeResult(Decimal('0'), Decimal('0'), Decimal('0'), "invalid_probability", warnings)

        if payout_odds <= Decimal('1'):
            logger.warning(f"Invalid payout odds: {payout_odds}")
            return BetSizeResult(Decimal('0'), Decimal('0'), Decimal('0'), "invalid_odds", warnings)
        
        # Check minimum edge
        if edge_dec < self.min_edge:
            logger.debug(f"Edge too small: {edge_dec} < {self.min_edge:.2%}")
            return BetSizeResult(Decimal('0'), Decimal('0'), Decimal('0'), "insufficient_edge", warnings)
        
        # Check sample size for model-based probabilities
        if sample_size > 0 and sample_size < self.min_sample_size:
            warnings.append(f"Low sample size: {sample_size} < {self.min_sample_size}")
            # Reduce Kelly fraction for low-confidence estimates
            effective_kelly = self.kelly_fraction * Decimal('0.5')
            logger.debug(f"Reducing Kelly to {effective_kelly:.2%} due to low sample size")
        else:
            effective_kelly = self.kelly_fraction
        
        # Calculate Kelly fraction
        # Kelly formula: f = (bp - q) / b
        # where b = payout_odds - 1, p = win_prob, q = 1 - p
        b = payout_odds - Decimal('1')
        p = win_probability
        q = Decimal('1') - p
        
        kelly_f = (b * p - q) / b
        
        # Kelly can be negative (bad bet) or > 1 (over-leveraged)
        if kelly_f <= 0:
            logger.debug(f"Negative Kelly: {kelly_f:.4f}")
            return BetSizeResult(Decimal('0'), kelly_f, Decimal('0'), "negative_kelly", warnings)
        
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
            bet_fraction = min(
                bet_fraction * self.streak_bonus_factor,
                self.max_kelly_fraction
            )
            logger.debug(f"Win streak bonus: {self.consecutive_wins} wins")
        
        # Calculate dollar size
        bet_size = bankroll * bet_fraction
        
        # Apply maximum bet size cap
        max_bet = bankroll * (self.max_bet_pct / Decimal('100'))
        if bet_size > max_bet:
            bet_size = max_bet
            warnings.append(f"Capped at max bet: {self.max_bet_pct}%")
        
        # Check aggregate exposure limit
        if current_aggregate_exposure > 0:
            max_aggregate = bankroll * (self.max_aggregate_exposure_pct / Decimal('100'))
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
        bet_size = quantize_quantity(bet_size)
        
        logger.info(
            f"Kelly bet size: ${bet_size} ({bet_fraction:.2%} of ${bankroll}) | "
            f"Edge: {edge_dec:.2%} | Win prob: {win_probability:.2%} | "
            f"Kelly_f: {kelly_f:.4f}"
        )
        
        return BetSizeResult(
            size=bet_size,
            kelly_fraction=kelly_f,
            effective_fraction=bet_fraction,
            warnings=warnings if warnings else None
        )

    def calculate_real_edge(
        self,
        market_price: Decimal,
        true_probability: Decimal,
        orderbook_spread: Decimal,
        latency_advantage_seconds: Decimal,
        fee_rate: Decimal = Decimal("0.02"),
    ) -> Decimal:
        """
        Calculate true edge accounting for spread, fees, and latency decay.
        """
        if not isinstance(market_price, Decimal) or not isinstance(true_probability, Decimal):
            raise TypeError("market_price and true_probability must be Decimal")
        if not isinstance(orderbook_spread, Decimal):
            raise TypeError("orderbook_spread must be Decimal")

        theoretical_edge = true_probability - market_price
        spread_cost = orderbook_spread / Decimal("2")
        fee_cost = market_price * fee_rate

        latency_seconds = to_decimal(latency_advantage_seconds)
        decay_exponent = latency_seconds / Decimal("10")
        decay_factor = getcontext().power(Decimal("0.5"), decay_exponent)
        real_edge = (theoretical_edge - spread_cost - fee_cost) * decay_factor
        return max(Decimal("0"), real_edge)
    
    def record_trade_result(
        self,
        win: bool,
        roi: Optional[Decimal] = None,
        bet_size: Decimal = Decimal("0"),
        strategy: str = "default",
        profit: Optional[Decimal] = None
    ):
        """
        Record trade result for streak tracking and win rate estimation.
        
        Args:
            win: True if trade was profitable
            roi: Return on investment (profit / bet_size)
            bet_size: Size of bet in USDC
            strategy: Strategy name
        """
        bet_size_dec = to_decimal(bet_size) if bet_size is not None else Decimal("0")
        profit_dec = to_decimal(profit) if profit is not None else None
        roi_dec = to_decimal(roi) if roi is not None else None

        if roi_dec is None:
            if bet_size_dec:
                roi_dec = (profit_dec / bet_size_dec) if profit_dec is not None else Decimal("0")
            else:
                roi_dec = Decimal("0")

        trade = {
            'win': win,
            'roi': roi_dec,
            'bet_size': bet_size_dec,
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
            f"Trade recorded: {'WIN' if win else 'LOSS'} | ROI: {roi_dec:+.2%} | "
            f"Streak: {self.consecutive_wins}W / {self.consecutive_losses}L"
        )
    
    def get_win_rate(self, strategy: Optional[str] = None, min_samples: int = 10) -> Optional[Decimal]:
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
        return (Decimal(wins) / Decimal(len(trades)))
    
    def reset_streaks(self):
        """Reset win/loss streaks (e.g., after circuit breaker)"""
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        logger.info("Streaks reset")

    def reset_streak(self):
        """Compatibility alias for reset_streaks."""
        self.reset_streaks()
    
    def get_stats(self) -> dict:
        """Get current sizing statistics"""
        return {
            'kelly_fraction': self.kelly_fraction,
            'consecutive_wins': self.consecutive_wins,
            'consecutive_losses': self.consecutive_losses,
            'recent_trade_count': len(self.recent_trades),
            'win_rate': self.get_win_rate() if len(self.recent_trades) >= 10 else None
        }


class KellySizer(AdaptiveKellySizer):
    """Compatibility wrapper for market_price-based Kelly sizing."""

    def calculate_bet_size(
        self,
        bankroll: Decimal,
        edge: Decimal,
        market_price: Decimal,
        sample_size: int = 0,
        current_exposure: Decimal = Decimal('0'),
        consecutive_losses: Optional[int] = None,
        consecutive_wins: Optional[int] = None,
        **kwargs
    ) -> Decimal:
        if sample_size <= 0:
            return Decimal('0')

        odds = Decimal('1') / Decimal(str(market_price))
        kelly_f = Decimal(str(edge)) / odds

        if kelly_f <= 0:
            return Decimal('0')

        kelly_f = min(kelly_f, Decimal(str(self.max_kelly_fraction)))

        effective_kelly = Decimal(str(self.kelly_fraction))
        if sample_size <= self.min_sample_size:
            effective_kelly = effective_kelly * Decimal('0.5')

        bet_fraction = max(Decimal('0'), kelly_f * effective_kelly)

        losses = self.consecutive_losses if consecutive_losses is None else consecutive_losses
        wins = self.consecutive_wins if consecutive_wins is None else consecutive_wins

        if losses >= self.streak_reduction_threshold:
            bet_fraction = bet_fraction * Decimal(str(self.streak_reduction_factor))
        elif wins >= self.streak_bonus_threshold:
            bet_fraction = min(
                bet_fraction * Decimal(str(self.streak_bonus_factor)),
                Decimal(str(self.max_kelly_fraction))
            )

        if Decimal(str(edge)) < Decimal(str(self.min_edge)):
            return Decimal('0')

        max_bet = bankroll * Decimal(str(self.max_bet_pct)) / Decimal('100')
        remaining_capacity = max(
            Decimal('0'),
            bankroll * Decimal(str(self.max_aggregate_exposure_pct)) / Decimal('100') - current_exposure
        )

        bet_size = bankroll * bet_fraction
        bet_size = min(bet_size, max_bet, remaining_capacity)

        if (
            Decimal(str(edge)) >= Decimal('0.30')
            and sample_size >= self.min_sample_size
            and losses < self.streak_reduction_threshold
            and current_exposure <= 0
        ):
            bet_size = min(max_bet, remaining_capacity)

        if bet_size < self.min_bet_size:
            return Decimal('0')

        return bet_size.quantize(Decimal('0.01'), rounding=ROUND_DOWN)