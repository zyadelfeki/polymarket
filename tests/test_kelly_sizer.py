#!/usr/bin/env python3
"""
Unit Tests for Kelly Criterion Position Sizer

Critical tests:
1. Fractional Kelly calculation
2. Safety caps enforcement (5% max per trade)
3. Minimum edge requirement (2%)
4. Aggregate exposure limit (20%)
5. Sample size adjustments
6. Loss streak reduction
7. Edge case handling
"""

import unittest
from decimal import Decimal
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from risk.kelly_sizer import KellySizer

class TestKellySizer(unittest.TestCase):
    """
    Test Kelly criterion position sizer.
    """
    
    def setUp(self):
        """Create KellySizer with default config"""
        self.sizer = KellySizer(config={
            'kelly_fraction': 0.25,  # 1/4 Kelly
            'max_bet_pct': 5.0,      # 5% max
            'min_edge': 0.02,        # 2% min edge
            'max_aggregate_exposure': 20.0  # 20% total
        })
    
    # ===========================================
    # Test 1: Basic Kelly Calculation
    # ===========================================
    
    def test_kelly_formula_correct(self):
        """Test Kelly formula: (edge / odds) * kelly_fraction"""
        # Edge: 10%, Odds: 2.0 (price 0.50)
        # Kelly = (0.10 / 2.0) * 0.25 = 0.0125 = 1.25%
        
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        # 1.25% of 10000 = 125
        expected = Decimal('125.00')
        self.assertAlmostEqual(float(bet_size), float(expected), places=2)
    
    def test_kelly_scales_with_edge(self):
        """Test bet size increases with edge"""
        bet_5pct_edge = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.05'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        bet_10pct_edge = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertGreater(bet_10pct_edge, bet_5pct_edge,
                          "Larger edge should produce larger bet")
    
    def test_kelly_scales_with_bankroll(self):
        """Test bet size scales proportionally with bankroll"""
        bet_10k = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        bet_20k = self.sizer.calculate_bet_size(
            bankroll=Decimal('20000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        # Should be exactly 2x
        self.assertAlmostEqual(float(bet_20k), float(bet_10k) * 2, places=2)
    
    # ===========================================
    # Test 2: Safety Cap Enforcement
    # ===========================================
    
    def test_max_bet_cap_enforced(self):
        """Test 5% maximum bet size is enforced"""
        # Use huge edge that would suggest large Kelly bet
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.50'),  # Huge 50% edge
            market_price=Decimal('0.50'),
            sample_size=1000,
            current_exposure=Decimal('0')
        )
        
        max_allowed = Decimal('10000') * Decimal('0.05')  # 5% = 500
        self.assertLessEqual(bet_size, max_allowed,
                            "Bet size must not exceed 5% cap")
    
    def test_aggregate_exposure_enforced(self):
        """Test 20% aggregate exposure limit"""
        # Current exposure: 15%
        # Should only allow 5% more (to reach 20% total)
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.20'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('1500')  # 15% of bankroll
        )
        
        max_additional = Decimal('10000') * Decimal('0.05')  # Only 5% more
        self.assertLessEqual(bet_size, max_additional,
                            "Must respect aggregate exposure limit")
    
    def test_zero_bet_when_at_aggregate_limit(self):
        """Test no bet when already at aggregate exposure limit"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.20'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('2000')  # Already at 20%
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Should not bet when at exposure limit")
    
    def test_zero_bet_when_over_aggregate_limit(self):
        """Test no bet when over aggregate exposure limit"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.20'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('2500')  # Over 20%
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Should not bet when over exposure limit")
    
    # ===========================================
    # Test 3: Minimum Edge Requirement
    # ===========================================
    
    def test_zero_edge_rejected(self):
        """Test zero edge produces zero bet"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.00'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Zero edge should produce zero bet")
    
    def test_negative_edge_rejected(self):
        """Test negative edge produces zero bet"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('-0.05'),  # Negative edge
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Negative edge should produce zero bet")
    
    def test_below_minimum_edge_rejected(self):
        """Test edge below 2% minimum produces zero bet"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.01'),  # 1% edge, below 2% minimum
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Edge below minimum should produce zero bet")
    
    def test_at_minimum_edge_accepted(self):
        """Test edge exactly at 2% minimum is accepted"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.02'),  # Exactly 2%
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertGreater(bet_size, Decimal('0'),
                          "Edge at minimum should be accepted")
    
    # ===========================================
    # Test 4: Sample Size Adjustments
    # ===========================================
    
    def test_low_sample_size_reduces_bet(self):
        """Test low sample size reduces bet size"""
        bet_high_samples = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=200,  # High confidence
            current_exposure=Decimal('0')
        )
        
        bet_low_samples = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=20,  # Low confidence
            current_exposure=Decimal('0')
        )
        
        self.assertLess(bet_low_samples, bet_high_samples,
                       "Low sample size should reduce bet")
    
    def test_zero_sample_size_rejected(self):
        """Test zero sample size produces zero bet"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=0,  # No data
            current_exposure=Decimal('0')
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Zero sample size should produce zero bet")
    
    # ===========================================
    # Test 5: Loss Streak Reduction
    # ===========================================
    
    def test_loss_streak_reduces_bet(self):
        """Test 3+ loss streak cuts bet to 50%"""
        # Normal bet (no losses)
        bet_no_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=0
        )
        
        # After 3 losses
        bet_with_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=3
        )
        
        # Should be 50% of normal
        expected_reduced = bet_no_losses * Decimal('0.5')
        self.assertAlmostEqual(float(bet_with_losses), float(expected_reduced), places=2)
    
    def test_one_loss_no_reduction(self):
        """Test 1 loss doesn't trigger reduction"""
        bet_no_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=0
        )
        
        bet_one_loss = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=1
        )
        
        self.assertEqual(bet_one_loss, bet_no_losses,
                        "One loss should not reduce bet")
    
    def test_two_losses_no_reduction(self):
        """Test 2 losses doesn't trigger reduction"""
        bet_no_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=0
        )
        
        bet_two_losses = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0'),
            consecutive_losses=2
        )
        
        self.assertEqual(bet_two_losses, bet_no_losses,
                        "Two losses should not reduce bet")
    
    # ===========================================
    # Test 6: Edge Cases
    # ===========================================
    
    def test_zero_bankroll_rejected(self):
        """Test zero bankroll produces zero bet"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('0'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        self.assertEqual(bet_size, Decimal('0'),
                        "Zero bankroll should produce zero bet")
    
    def test_extreme_price_low(self):
        """Test extreme low price (0.01) handled correctly"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.01'),  # Near minimum
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        # Should still produce reasonable bet
        self.assertGreater(bet_size, Decimal('0'))
        self.assertLessEqual(bet_size, Decimal('500'))  # Max 5%
    
    def test_extreme_price_high(self):
        """Test extreme high price (0.99) handled correctly"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.10'),
            market_price=Decimal('0.99'),  # Near maximum
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        # Should still produce reasonable bet
        self.assertGreater(bet_size, Decimal('0'))
        self.assertLessEqual(bet_size, Decimal('500'))  # Max 5%
    
    def test_huge_edge_capped(self):
        """Test unrealistically huge edge is capped"""
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.90'),  # Unrealistic 90% edge
            market_price=Decimal('0.50'),
            sample_size=100,
            current_exposure=Decimal('0')
        )
        
        # Should be capped at 5%
        max_allowed = Decimal('500')  # 5% of 10000
        self.assertLessEqual(bet_size, max_allowed)
    
    # ===========================================
    # Test 7: Combined Constraints
    # ===========================================
    
    def test_multiple_constraints_applied(self):
        """Test multiple constraints stack correctly"""
        # Small edge (2%), low samples (30), 1 prior loss, some exposure (10%)
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.02'),  # Minimum edge
            market_price=Decimal('0.50'),
            sample_size=30,  # Low confidence
            current_exposure=Decimal('1000'),  # 10% already used
            consecutive_losses=1  # But below 3, so no reduction
        )
        
        # Should produce small bet
        self.assertGreater(bet_size, Decimal('0'), "Should allow bet")
        self.assertLess(bet_size, Decimal('100'), "Should be conservative")
    
    def test_perfect_conditions_max_bet(self):
        """Test perfect conditions produce maximum bet"""
        # Large edge, high samples, no losses, no exposure
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.30'),  # Large edge
            market_price=Decimal('0.50'),
            sample_size=500,  # High confidence
            current_exposure=Decimal('0'),  # No exposure
            consecutive_losses=0  # No losses
        )
        
        # Should hit 5% cap
        expected_max = Decimal('500')  # 5% of 10000
        self.assertEqual(bet_size, expected_max)
    
    def test_worst_conditions_zero_bet(self):
        """Test worst conditions produce zero bet"""
        # No edge, low samples, losses, at exposure limit
        bet_size = self.sizer.calculate_bet_size(
            bankroll=Decimal('10000'),
            edge=Decimal('0.00'),  # No edge
            market_price=Decimal('0.50'),
            sample_size=5,  # Low confidence
            current_exposure=Decimal('2000'),  # At limit
            consecutive_losses=5  # Many losses
        )
        
        self.assertEqual(bet_size, Decimal('0'))

if __name__ == '__main__':
    unittest.main()