"""Test confidence boosting with mock intelligence signals"""
import pytest
from decimal import Decimal
from strategies.confidence_booster import ConfidenceBooster


class MockRedisSubscriber:
    def __init__(self, intelligence_data):
        self.data = intelligence_data

    def get_intelligence(self):
        return self.data


def test_lstm_boost_applied():
    """Test LSTM prediction adds 10% confidence boost"""
    mock_redis = MockRedisSubscriber({
        'timestamp': 1234567890,
        'lstm_prediction': 'UP',
        'lstm_confidence': 0.85,
        'whale_flow': 0.0,
        'mev_volatility': 0.3
    })

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.90")

    # LSTM predicts UP with 85% confidence → +8.5% boost
    boosted = booster.apply_boost(base, trade_direction="UP")

    expected = base + Decimal("0.85") * Decimal("0.10")  # 0.90 + 0.085 = 0.985
    assert abs(boosted - expected) < Decimal("0.001"), f"Expected {expected}, got {boosted}"
    print(f"✅ LSTM boost: {base} → {boosted} (+{boosted - base:.3f})")


def test_whale_confirmation_boost():
    """Test whale inflow adds 5% confidence"""
    mock_redis = MockRedisSubscriber({
        'timestamp': 1234567890,
        'lstm_prediction': 'DOWN',  # Opposite direction - no LSTM boost
        'lstm_confidence': 0.75,
        'whale_flow': 5000000.0,  # Positive = buying pressure
        'mev_volatility': 0.4
    })

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.90")

    # Whale flow confirms UP direction → +5% boost
    boosted = booster.apply_boost(base, trade_direction="UP")

    expected = base + Decimal("0.05")  # 0.90 + 0.05 = 0.95
    assert boosted == expected, f"Expected {expected}, got {boosted}"
    print(f"✅ Whale boost: {base} → {boosted} (+{boosted - base:.3f})")


def test_mev_volatility_penalty():
    """Test high MEV volatility reduces confidence by 5%"""
    mock_redis = MockRedisSubscriber({
        'timestamp': 1234567890,
        'lstm_prediction': 'UP',
        'lstm_confidence': 0.80,
        'whale_flow': 0.0,
        'mev_volatility': 0.85  # High volatility → penalty
    })

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.90")

    # LSTM boost +8%, MEV penalty -5% → Net +3%
    boosted = booster.apply_boost(base, trade_direction="UP")

    expected = base + Decimal("0.08") - Decimal("0.05")  # 0.90 + 0.03 = 0.93
    assert abs(boosted - expected) < Decimal("0.001"), f"Expected {expected}, got {boosted}"
    print(f"✅ MEV penalty applied: {base} → {boosted} (net {boosted - base:+.3f})")


def test_all_signals_aligned_bullish():
    """Test maximum boost scenario: all signals confirm UP"""
    mock_redis = MockRedisSubscriber({
        'timestamp': 1234567890,
        'lstm_prediction': 'UP',
        'lstm_confidence': 0.90,  # 90% confidence → +9% boost
        'whale_flow': 10000000.0,  # Strong buying → +5% boost
        'mev_volatility': 0.3  # Low volatility → no penalty
    })

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.95")

    # LSTM +9%, Whale +5% → Total +14% → Clamped at 1.0
    boosted = booster.apply_boost(base, trade_direction="UP")

    assert boosted == Decimal("1.0"), f"Should be clamped at 1.0, got {boosted}"
    print(f"✅ Max boost (clamped): {base} → {boosted}")


def test_conflicting_signals():
    """Test when LSTM disagrees with price direction"""
    mock_redis = MockRedisSubscriber({
        'timestamp': 1234567890,
        'lstm_prediction': 'DOWN',  # LSTM bearish
        'lstm_confidence': 0.80,
        'whale_flow': 8000000.0,  # But whales are buying (conflicting)
        'mev_volatility': 0.5
    })

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.92")

    # Price says UP, but LSTM says DOWN → No LSTM boost
    # Whale confirms UP → +5%
    boosted = booster.apply_boost(base, trade_direction="UP")

    expected = base + Decimal("0.05")  # Only whale boost
    assert boosted == expected, f"Expected {expected}, got {boosted}"
    print(f"✅ Conflicting signals: {base} → {boosted} (only whale boost)")


def test_no_intelligence_available():
    """Test fallback when Redis has no data"""
    mock_redis = MockRedisSubscriber(None)  # No intelligence data

    booster = ConfidenceBooster(mock_redis)
    base = Decimal("0.88")

    boosted = booster.apply_boost(base, trade_direction="UP")

    assert boosted == base, "Should return base confidence when no intel"
    print(f"✅ No intelligence: {base} → {boosted} (unchanged)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
