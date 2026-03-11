from decimal import Decimal

import pytest

import integrations.charlie_booster as charlie_booster
from integrations.charlie_booster import CharlieContractError, CharliePredictionGate


@pytest.mark.asyncio
async def test_coin_flip_reject_band_can_be_narrowed(monkeypatch):
    async def fake_signal(_symbol, _timeframe, extra_features=None):
        return {
            "contract_version": "1.0",
            "p_win": 0.5,
            "confidence": 0.7624,
            "regime": "NEUTRAL",
            "technical_regime": "TRENDING",
            "lstm_direction": "UP",
            "lstm_confidence": 0.7624,
            "model_votes": None,
        }

    monkeypatch.setattr(charlie_booster, "_get_signal_for_market", fake_signal)

    default_gate = CharliePredictionGate(min_edge=Decimal("0.10"))
    default_recommendation = await default_gate.evaluate_market(
        market_id="1510215",
        market_price=Decimal("0.77"),
        bankroll=Decimal("100"),
        market_question="Bitcoin Up or Down - March 6, 3:45PM-4:00PM ET",
    )
    assert default_recommendation is None

    relaxed_gate = CharliePredictionGate(
        min_edge=Decimal("0.10"),
        coin_flip_reject_band_abs=Decimal("0.0"),
    )
    relaxed_recommendation = await relaxed_gate.evaluate_market(
        market_id="1510215",
        market_price=Decimal("0.77"),
        bankroll=Decimal("100"),
        market_question="Bitcoin Up or Down - March 6, 3:45PM-4:00PM ET",
    )
    assert relaxed_recommendation is not None
    assert relaxed_recommendation.side == "NO"


@pytest.mark.asyncio
async def test_verify_contract_health_fails_on_missing_required_fields(monkeypatch):
    async def bad_signal(_symbol, _timeframe, extra_features=None):
        return {
            "contract_version": "1.0",
            "p_win": 0.61,
            "confidence": 0.73,
            "regime": "BULLISH",
            "technical_regime": "TRENDING",
        }

    monkeypatch.setattr(charlie_booster, "_get_signal_for_market", bad_signal)

    gate = CharliePredictionGate(min_edge=Decimal("0.05"))
    with pytest.raises(CharlieContractError):
        await gate.verify_contract_health()


@pytest.mark.asyncio
async def test_verify_contract_health_supports_keyword_signature(monkeypatch):
    async def keyword_signal(*, market_id, symbol, timeframe, extra_features=None):
        assert market_id == "startup_contract_probe"
        assert symbol == "BTC"
        assert timeframe == "15m"
        return {
            "contract_version": "1.0",
            "p_win": 0.64,
            "confidence": 0.81,
            "regime": "BULLISH",
            "technical_regime": "TRENDING",
            "lstm_direction": "UP",
            "lstm_confidence": 0.81,
        }

    monkeypatch.setattr(charlie_booster, "_get_signal_for_market", keyword_signal)

    gate = CharliePredictionGate(min_edge=Decimal("0.05"))
    signal = await gate.verify_contract_health()
    assert signal["contract_version"] == "1.0"
