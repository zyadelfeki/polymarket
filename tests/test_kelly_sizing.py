import pytest
from decimal import Decimal
from risk.kelly_sizer import AdaptiveKellySizer

class TestKellySizing:
    
    def setup_method(self):
        self.sizer = AdaptiveKellySizer()
    
    def test_kelly_calculation(self):
        """Test Kelly criterion sizing"""
        
        size = self.sizer.calculate_bet_size(
            bankroll=Decimal('1000'),
            win_probability=0.70,
            payout_odds=2.0,
            edge=0.15
        )
        
        # Kelly: f = (p*b - q) / b
        # f = (0.70*2.0 - 0.30) / 2.0 = 1.10 / 2.0 = 0.55 (55%)
        # But Kelly is typically fractional for crypto (e.g., 1/4 Kelly = 13.75%)
        assert size > 0
        assert size < Decimal('1000')  # Can't bet more than bankroll
    
    def test_max_position_limit(self):
        """Test that bets don't exceed max position size"""
        
        size = self.sizer.calculate_bet_size(
            bankroll=Decimal('1000'),
            win_probability=0.90,
            payout_odds=1.5,
            edge=0.30
        )
        
        # Even with 90% win prob, shouldn't exceed 20% of bankroll
        assert size <= Decimal('200')  # 20% of 1000
    
    def test_streak_adjustment(self):
        """Test position sizing adjusts on win/loss streaks"""
        
        # Record 3 wins
        for _ in range(3):
            self.sizer.record_trade_result(win=True, profit=10)
        
        size_after_wins = self.sizer.calculate_bet_size(
            bankroll=Decimal('1000'),
            win_probability=0.70,
            payout_odds=2.0,
            edge=0.15
        )
        
        # Reset and record 3 losses
        self.sizer.reset_streak()
        for _ in range(3):
            self.sizer.record_trade_result(win=False, profit=-10)
        
        size_after_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('1000'),
            win_probability=0.70,
            payout_odds=2.0,
            edge=0.15
        )
        
        # Size after wins should be larger
        assert size_after_wins > size_after_losses
    
    def test_zero_edge(self):
        """Test sizing with zero edge"""
        
        size = self.sizer.calculate_bet_size(
            bankroll=Decimal('1000'),
            win_probability=0.50,
            payout_odds=2.0,
            edge=0.0
        )
        
        # Zero edge = don't trade
        assert size == Decimal('0')

if __name__ == '__main__':
    pytest.main([__file__, '-v'])