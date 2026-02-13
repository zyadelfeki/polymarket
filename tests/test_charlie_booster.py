import pytest

from integrations.charlie_booster import CharliePredictionBooster


class StubIntel:
    def __init__(self, signal):
        self.signal = signal

    async def get_signal(self):
        return self.signal


@pytest.mark.asyncio
async def test_charlie_booster_veto():
    booster = CharliePredictionBooster(intelligence=StubIntel({"lstm_direction": "DOWN", "lstm_confidence": 0.8}))
    latency_signal = {"side": "YES"}
    allowed = await booster.should_trade(latency_signal)
    assert allowed is False


@pytest.mark.asyncio
async def test_charlie_booster_allows():
    booster = CharliePredictionBooster(intelligence=StubIntel({"lstm_direction": "UP", "lstm_confidence": 0.8}))
    latency_signal = {"side": "YES"}
    allowed = await booster.should_trade(latency_signal)
    assert allowed is True
