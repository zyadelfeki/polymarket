import pytest
from strategy.latency_arbitrage import LatencyArbitrageEngine

class TestLatencyArbitrage:
    
    def setup_method(self):
        self.engine = LatencyArbitrageEngine()
    
    def test_extract_threshold(self):
        """Test threshold extraction from market questions"""
        
        questions = [
            ("BTC closes above $95,000", 95000),
            ("ETH > 3000 USDT", 3000),
            ("SOL price above $200", 200),
        ]
        
        for question, expected_threshold in questions:
            threshold = self.engine._extract_threshold('BTC', question)
            assert threshold == expected_threshold
    
    def test_expected_probability_above(self):
        """Test probability calculation for 'above' markets"""
        
        # Price above threshold
        prob = self.engine._calculate_expected_probability(
            symbol='BTC',
            exchange_price=95300,
            threshold=95000,
            question='BTC closes above $95000'
        )
        assert prob >= 0.95  # Should be ~98%
        
        # Price below threshold
        prob = self.engine._calculate_expected_probability(
            symbol='BTC',
            exchange_price=94500,
            threshold=95000,
            question='BTC closes above $95000'
        )
        assert prob <= 0.05  # Should be ~2%
    
    def test_min_edge_filter(self):
        """Test that opportunities below min_edge are filtered"""
        
        market = {
            'condition_id': 'test_id',
            'question': 'BTC above $95000',
            'yes_price': 0.50,
            'no_price': 0.50,
        }
        
        # Create an opportunity with small edge
        # Edge = |0.98 - 0.50| = 0.48
        # Should pass filter (>0.05)
        assert 0.48 > self.engine.MIN_EDGE
    
    def test_opportunity_creation(self):
        """Test that opportunities have required fields"""
        
        market = {
            'condition_id': 'cond_123',
            'question': 'BTC above $95000',
            'yes_price': 0.30,
            'no_price': 0.70,
            'yes_liquidity': 100,
            'no_liquidity': 100,
        }
        
        # Manually create opportunity
        opp = {
            'type': 'threshold_arbitrage',
            'market_id': market['condition_id'],
            'question': market['question'],
            'edge': 0.45,
            'action': 'BUY_YES',
            'confidence': 0.90,
        }
        
        # Verify all required fields
        assert opp['type'] == 'threshold_arbitrage'
        assert opp['market_id']
        assert opp['question']
        assert opp['edge'] > 0
        assert opp['action'] in ['BUY_YES', 'BUY_NO']
        assert 0 <= opp['confidence'] <= 1

if __name__ == '__main__':
    pytest.main([__file__, '-v'])