from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from strategies.btc_price_level_scanner import BTCPriceLevelScanner


class StubCharlieGate:
    def __init__(self, recommendation=None):
        self.recommendation = recommendation
        self.calls = []

    async def evaluate_market(self, **kwargs):
        self.calls.append(kwargs)
        return self.recommendation


class StubApiClient:
    def __init__(self, markets):
        self.markets = markets
        self.calls = []
        self.orderbook_calls = []

    async def get_markets(self, *, active=True, limit=200):
        self.calls.append(("get_markets", active, limit))
        return list(self.markets)

    async def get_market_orderbook_summary(self, market_id):
        self.orderbook_calls.append(market_id)
        return {"bid": None, "ask": None}


def _market(*, question, yes_price="0.42", no_price="0.58", days=3, market_id="m1"):
    end_dt = datetime.now(timezone.utc) + timedelta(days=days)
    return {
        "id": market_id,
        "question": question,
        "end_date": end_dt.isoformat(),
        "tokens": [
            {"token_id": f"{market_id}_yes", "outcome": "Yes", "price": yes_price},
            {"token_id": f"{market_id}_no", "outcome": "No", "price": no_price},
        ],
    }


@pytest.mark.asyncio
async def test_btc_price_scanner_returns_empty_when_no_markets():
    scanner = BTCPriceLevelScanner()
    api_client = StubApiClient([])
    gate = StubCharlieGate()

    opportunities = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert opportunities == []
    assert gate.calls == []


@pytest.mark.asyncio
async def test_btc_price_scanner_filters_markets_rejected_by_charlie_gate():
    scanner = BTCPriceLevelScanner()
    api_client = StubApiClient([
        _market(question="Will Bitcoin price exceed $120,000 by Friday?", market_id="btc_1"),
    ])
    gate = StubCharlieGate(recommendation=None)

    opportunities = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert opportunities == []
    assert len(gate.calls) == 1
    assert gate.calls[0]["market_question"].startswith("Will Bitcoin price exceed")


@pytest.mark.asyncio
async def test_btc_price_scanner_surfaces_valid_mispriced_market():
    scanner = BTCPriceLevelScanner()
    api_client = StubApiClient([
        _market(question="Will BTC price exceed $110,000 by Friday?", yes_price="0.35", no_price="0.65", market_id="btc_2"),
    ])
    gate = StubCharlieGate(
        recommendation=SimpleNamespace(
            side="YES",
            size=Decimal("2.50"),
            kelly_fraction=Decimal("0.125"),
            p_win=0.68,
            p_win_raw=0.68,
            p_win_calibrated=0.68,
            implied_prob=0.35,
            edge=0.33,
            confidence=0.84,
            regime="BULLISH",
            technical_regime="TRENDING",
            reason="edge_pass",
            model_votes={"rf": "BUY"},
            ofi_conflict=False,
        )
    )

    opportunities = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity["market_id"] == "btc_2"
    assert opportunity["token_id"] == "btc_2_yes"
    assert opportunity["side"] == "YES"
    assert opportunity["size"] == Decimal("2.50")
    assert opportunity["edge"] == Decimal("0.33")
    assert opportunity["question"].startswith("Will BTC price exceed")


@pytest.mark.asyncio
async def test_btc_price_scanner_skips_irrelevant_non_btc_market():
    scanner = BTCPriceLevelScanner()
    api_client = StubApiClient([
        _market(question="Will Ethereum price exceed $8,000 by Friday?", market_id="eth_1"),
    ])
    gate = StubCharlieGate(recommendation=None)

    opportunities = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert opportunities == []
    assert gate.calls == []


@pytest.mark.asyncio
async def test_btc_price_scanner_caches_closed_market_ids_for_early_skip():
    scanner = BTCPriceLevelScanner()
    api_client = StubApiClient([
        {
            **_market(question="Will BTC price exceed $120,000 by Friday?", market_id="btc_closed"),
            "status": "closed",
        },
    ])
    gate = StubCharlieGate(recommendation=None)

    opportunities = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert opportunities == []
    assert "btc_closed" in scanner._closed_market_ids
    assert gate.calls == []


@pytest.mark.asyncio
async def test_btc_price_scanner_recent_rejection_avoids_repeat_orderbook_fetch():
    scanner = BTCPriceLevelScanner({"recent_rejection_ttl_seconds": 300})
    market = _market(
        question="Will BTC price exceed $120,000 by Friday?",
        yes_price=None,
        no_price=None,
        market_id="btc_no_price",
    )
    market["tokens"] = [
        {"token_id": "btc_no_price_yes", "outcome": "Yes", "price": None},
        {"token_id": "btc_no_price_no", "outcome": "No", "price": None},
    ]
    api_client = StubApiClient([market])
    gate = StubCharlieGate(recommendation=None)

    first = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )
    second = await scanner.scan(
        charlie_gate=gate,
        api_client=api_client,
        equity=Decimal("20"),
    )

    assert first == []
    assert second == []
    assert api_client.orderbook_calls == ["btc_no_price"]


# ---------------------------------------------------------------------------
# Task 3 — Binance features must expose all keys required by regime_guard
# ---------------------------------------------------------------------------

def test_binance_features_includes_regime_guard_keys():
    """
    get_candle_features() must return price, rsi_14, price_change_1h, and atr_pct.
    These are required by regime_guard.get_regime_verdict(); without them the
    LLM always receives zeros and the regime analysis is meaningless.
    """
    from data_feeds.binance_features import get_candle_features

    # 30 candles is enough to compute all features (need ≥ 27 for EMA-26)
    fake_closes = [84000.0 + float(i) * 10 for i in range(30)]

    with patch("data_feeds.binance_features._fetch_candles", return_value=fake_closes):
        features = get_candle_features("BTC")

    assert features is not None, "get_candle_features must return a dict for valid data"
    required_keys = {"price", "rsi_14", "price_change_1h", "atr_pct"}
    missing = required_keys - features.keys()
    assert not missing, f"Missing regime guard keys in binance_features: {missing}"

    # Sanity-check values are non-trivially default
    assert features["price"] > 0, "price must be the last close, not zero"
    assert features["atr_pct"] > 0, "atr_pct must be positive"