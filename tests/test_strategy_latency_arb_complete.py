from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


class StubExecution:
    def __init__(self, balance: Decimal = Decimal("100")):
        self._balance = balance
        self.last_order = None

    async def get_real_balance(self):
        return self._balance

    async def place_order(self, **kwargs):
        self.last_order = kwargs
        return {"ok": True, **kwargs}


class StubKelly:
    def __init__(self, size: Decimal = Decimal("2.5")):
        self.size = size

    def calculate_size(self, **_kwargs):
        return self.size


class StubBinanceCurrent:
    def __init__(self, price: Decimal):
        self.price = price

    def get_current_price(self, _symbol: str):
        return self.price


class StubBinancePrice:
    def __init__(self, price: Decimal):
        self.price = price

    async def get_price(self, _symbol: str):
        return self.price


class StubBinancePriceData:
    def __init__(self, price: Decimal):
        self.price = price

    async def get_price_data(self, _symbol: str):
        return SimpleNamespace(price=self.price)


class StubPolymarket:
    def __init__(self):
        self.get_active_markets = AsyncMock(return_value=[])
        self.get_markets = AsyncMock(return_value=[])
        self.get_orderbook = AsyncMock(return_value=None)
        self.get_market_orderbook_summary = AsyncMock(return_value=None)


def make_engine(
    *,
    binance=None,
    polymarket=None,
    charlie=None,
    execution=None,
    kelly=None,
    config=None,
):
    return LatencyArbitrageEngine(
        binance_ws=binance or StubBinanceCurrent(Decimal("96000")),
        polymarket_client=polymarket or StubPolymarket(),
        charlie_predictor=charlie,
        config=config,
        execution_service=execution,
        kelly_sizer=kelly,
        redis_subscriber=None,
    )


@pytest.mark.asyncio
async def test_execute_signal_success_path_yes():
    polymarket = StubPolymarket()
    polymarket.get_orderbook.return_value = {"asks": [["0.42", "100"]]}
    execution = StubExecution()
    kelly = StubKelly(Decimal("3.333"))
    engine = make_engine(polymarket=polymarket, execution=execution, kelly=kelly)

    market = {
        "id": "m1",
        "tokens": [
            {"outcome": "YES", "token_id": "yes1", "price": "0.42"},
            {"outcome": "NO", "token_id": "no1", "price": "0.58"},
        ],
    }

    result = await engine.execute_signal(market=market, signal="BULLISH", confidence=Decimal("0.7"))

    assert result is not None
    assert execution.last_order is not None
    assert execution.last_order["token_id"] == "yes1"
    assert execution.last_order["side"] == "BUY"


@pytest.mark.asyncio
async def test_execute_signal_rejects_missing_components_and_invalid_signal():
    engine = make_engine()
    market = {"id": "m1", "tokens": []}
    assert await engine.execute_signal(market=market, signal="BULLISH", confidence=Decimal("0.6")) is None

    polymarket = StubPolymarket()
    polymarket.get_orderbook.return_value = {"asks": [["0.5", "10"]]}
    engine = make_engine(polymarket=polymarket, execution=StubExecution(), kelly=StubKelly())
    market = {
        "id": "m2",
        "tokens": [
            {"outcome": "YES", "token_id": "yes2", "price": "0.4"},
            {"outcome": "NO", "token_id": "no2", "price": "0.6"},
        ],
    }
    assert await engine.execute_signal(market=market, signal="SIDEWAYS", confidence=Decimal("0.6")) is None


@pytest.mark.asyncio
async def test_execute_signal_handles_no_liquidity_and_missing_tokens():
    polymarket = StubPolymarket()
    polymarket.get_orderbook.return_value = {"asks": []}
    engine = make_engine(polymarket=polymarket, execution=StubExecution(), kelly=StubKelly())

    market_no_tokens = {"id": "m3", "tokens": []}
    assert await engine.execute_signal(market=market_no_tokens, signal="BULLISH", confidence=Decimal("0.7")) is None

    market = {
        "id": "m4",
        "tokens": [
            {"outcome": "YES", "token_id": "yes4", "price": "0.4"},
            {"outcome": "NO", "token_id": "no4", "price": "0.6"},
        ],
    }
    assert await engine.execute_signal(market=market, signal="BEARISH", confidence=Decimal("0.7")) is None


def test_determine_trade_direction_paths():
    engine = make_engine()

    assert engine.determine_trade_direction(
        btc_price=Decimal("97000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.5"),
        no_odds=Decimal("0.5"),
    )["direction"] == "BULLISH"

    assert engine.determine_trade_direction(
        btc_price=Decimal("95000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.5"),
        no_odds=Decimal("0.2"),
    )["direction"] == "BEARISH"

    assert engine.determine_trade_direction(
        btc_price=Decimal("96000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.5"),
        no_odds=Decimal("0.5"),
    ) is None

    assert engine.determine_trade_direction(
        btc_price=Decimal("96000"),
        strike_price=Decimal("0"),
        yes_odds=Decimal("0.5"),
        no_odds=Decimal("0.5"),
    ) is None


@pytest.mark.asyncio
async def test_scan_opportunities_returns_none_when_no_btc_or_markets():
    class NoPriceBinance:
        async def get_price(self, _symbol: str):
            return None

    polymarket = StubPolymarket()
    engine = make_engine(binance=NoPriceBinance(), polymarket=polymarket)
    assert await engine.scan_opportunities() is None

    engine2 = make_engine(polymarket=polymarket)
    polymarket.get_active_markets.return_value = []
    assert await engine2.scan_opportunities() is None


@pytest.mark.asyncio
async def test_scan_opportunities_finds_yes_opportunity():
    polymarket = StubPolymarket()
    polymarket.get_active_markets.return_value = [
        {
            "id": "m-op-1",
            "question": "Will BTC be above $96,000 in 15 minutes?",
            "startingPrice": "95000",
            "tokens": [
                {"outcome": "YES", "token_id": "y1", "price": "0.40"},
                {"outcome": "NO", "token_id": "n1", "price": "0.60"},
            ],
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        }
    ]
    engine = make_engine(polymarket=polymarket, binance=StubBinanceCurrent(Decimal("98000")))

    opp = await engine.scan_opportunities()
    assert opp is not None
    assert opp["side"] == "YES"


@pytest.mark.asyncio
async def test_scan_opportunities_finds_no_opportunity():
    polymarket = StubPolymarket()
    polymarket.get_active_markets.return_value = [
        {
            "id": "m-op-2",
            "question": "Will BTC be above $96,000 in 15 minutes?",
            "startingPrice": "98000",
            "tokens": [
                {"outcome": "YES", "token_id": "y2", "price": "0.80"},
                {"outcome": "NO", "token_id": "n2", "price": "0.20"},
            ],
            "endDate": (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat().replace("+00:00", "Z"),
        }
    ]
    engine = make_engine(polymarket=polymarket, binance=StubBinanceCurrent(Decimal("95000")))

    opp = await engine.scan_opportunities()
    assert opp is not None
    assert opp["side"] == "NO"


@pytest.mark.asyncio
async def test_find_15min_market_filters_and_selects_closest_threshold():
    polymarket = StubPolymarket()
    now = datetime.now(timezone.utc)
    polymarket.get_active_markets.return_value = [
        {
            "id": "bad-asset",
            "question": "Will ETH be above $3,000 in 15 minutes?",
            "tokens": [{"outcome": "YES", "token_id": "y"}, {"outcome": "NO", "token_id": "n"}],
            "endDate": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        },
        {
            "id": "far-threshold",
            "question": "Will BTC be above $100,000 in 15 minutes?",
            "tokens": [{"outcome": "YES", "token_id": "y1"}, {"outcome": "NO", "token_id": "n1"}],
            "endDate": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        },
        {
            "id": "closest-threshold",
            "question": "Will BTC be above $96,000 in 15 minutes?",
            "tokens": [{"outcome": "YES", "token_id": "y2"}, {"outcome": "NO", "token_id": "n2"}],
            "endDate": (now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "status": "OPEN",
        },
    ]

    engine = make_engine(polymarket=polymarket)
    result = await engine.find_15min_market(Decimal("96100"))

    assert result is not None
    assert result["market"]["id"] == "closest-threshold"


@pytest.mark.asyncio
async def test_check_market_arbitrage_handles_non_btc_and_missing_prices_paths():
    polymarket = StubPolymarket()
    engine = make_engine(polymarket=polymarket)

    non_btc_market = {
        "id": "eth-1",
        "question": "Will ETH be above $3000 in 15 minutes?",
        "tokens": [{"outcome": "YES", "token_id": "y"}, {"outcome": "NO", "token_id": "n"}],
        "startingPrice": "3000",
    }
    assert await engine._check_market_arbitrage(non_btc_market, Decimal("3200")) is None

    missing_tokens_market = {
        "id": "btc-1",
        "question": "Will BTC be above $96000 in 15 minutes?",
        "tokens": [{"outcome": "YES", "price": "0.4"}],
        "startingPrice": "95000",
    }
    assert await engine._check_market_arbitrage(missing_tokens_market, Decimal("96000")) is None

    no_prices_market = {
        "id": "btc-2",
        "question": "Will BTC be above $96000 in 15 minutes?",
        "tokens": [{"outcome": "YES", "token_id": "y"}, {"outcome": "NO", "token_id": "n"}],
        "startingPrice": "95000",
    }
    polymarket.get_orderbook.return_value = None
    assert await engine._check_market_arbitrage(no_prices_market, Decimal("96000")) is None


@pytest.mark.asyncio
async def test_check_market_arbitrage_handles_start_price_and_edge_branches():
    polymarket = StubPolymarket()
    charlie = MagicMock()
    charlie.predict_15min_move = AsyncMock(return_value={"probability": "0.80", "confidence": "0.90"})

    engine = make_engine(polymarket=polymarket, charlie=charlie)

    neutral_market = {
        "id": "m-neutral",
        "question": "Will BTC be above $96,000 in 15 minutes?",
        "startingPrice": "100000",
        "tokens": [
            {"outcome": "YES", "token_id": "y", "price": "0.50"},
            {"outcome": "NO", "token_id": "n", "price": "0.50"},
        ],
    }
    assert await engine._check_market_arbitrage(neutral_market, Decimal("100010")) is None

    positive_edge_market = {
        "id": "m-pos",
        "question": "Will BTC be above $96,000 in 15 minutes?",
        "startingPrice": "95000",
        "tokens": [
            {"outcome": "YES", "token_id": "y1", "price": "0.40"},
            {"outcome": "NO", "token_id": "n1", "price": "0.60"},
        ],
    }
    yes_opp = await engine._check_market_arbitrage(positive_edge_market, Decimal("98000"))
    assert yes_opp is not None and yes_opp["side"] == "YES"

    negative_edge_market = {
        "id": "m-neg",
        "question": "Will BTC be above $96,000 in 15 minutes?",
        "startingPrice": "98000",
        "tokens": [
            {"outcome": "YES", "token_id": "y2", "price": "0.85"},
            {"outcome": "NO", "token_id": "n2", "price": "0.20"},
        ],
    }
    no_opp = await engine._check_market_arbitrage(negative_edge_market, Decimal("95000"))
    assert no_opp is not None and no_opp["side"] == "NO"

    missing_start_market = {
        "id": "m-missing-start",
        "question": "Will BTC be above $96,000 in 15 minutes?",
        "tokens": [
            {"outcome": "YES", "token_id": "y3", "price": "0.50"},
            {"outcome": "NO", "token_id": "n3", "price": "0.50"},
        ],
    }
    assert await engine._check_market_arbitrage(missing_start_market, Decimal("96000")) is None


@pytest.mark.asyncio
async def test_get_btc_price_all_fallbacks():
    engine_sync = make_engine(binance=StubBinanceCurrent(Decimal("97000")))
    assert await engine_sync._get_btc_price() == Decimal("97000")

    class CurrentNoneAndPrice(StubBinancePrice):
        def get_current_price(self, _symbol: str):
            return None

    engine_async = make_engine(binance=CurrentNoneAndPrice(Decimal("97100")))
    assert await engine_async._get_btc_price() == Decimal("97100")

    class CurrentNonePriceNone(StubBinancePriceData):
        def get_current_price(self, _symbol: str):
            return None

        async def get_price(self, _symbol: str):
            return None

    engine_data = make_engine(binance=CurrentNonePriceNone(Decimal("97200")))
    assert await engine_data._get_btc_price() == Decimal("97200")


@pytest.mark.asyncio
async def test_get_active_markets_cache_and_fallback_get_markets():
    polymarket = SimpleNamespace(
        get_markets=AsyncMock(return_value=[{"id": "m1"}, "bad-row"])
    )
    engine = make_engine(polymarket=polymarket)

    first = await engine._get_active_markets()
    second = await engine._get_active_markets()

    assert first == [{"id": "m1"}]
    assert second == [{"id": "m1"}]
    assert polymarket.get_markets.await_count == 1


@pytest.mark.asyncio
async def test_get_market_prices_and_orderbook_helpers():
    polymarket = StubPolymarket()
    polymarket.get_market_orderbook_summary.return_value = {"ask": "0.44"}
    polymarket.get_orderbook.return_value = {
        "bids": [{"price": "0.55"}],
        "asks": [{"price": "0.65"}],
    }
    engine = make_engine(polymarket=polymarket)

    market = {
        "id": "m-prices",
        "tokens": [
            {"outcome": "YES", "token_id": "y", "price": "0.40"},
            {"outcome": "NO", "token_id": "n", "price": "0.60"},
        ],
    }

    yes, no = await engine._get_market_prices(market=market, yes_token_id="y", no_token_id="n")
    assert yes == Decimal("0.44")
    assert no == Decimal("0.60")

    market_missing_no = {
        "id": "m-prices-2",
        "tokens": [{"outcome": "YES", "token_id": "y", "price": "0.40"}],
    }
    yes2, no2 = await engine._get_market_prices(market=market_missing_no, yes_token_id="y", no_token_id="n")
    assert yes2 == Decimal("0.44")
    assert no2 == Decimal("0.60")

    ob = await engine._get_orderbook_prices(yes_token_id="y", no_token_id="n", market_id="m-prices")
    assert ob is not None


@pytest.mark.asyncio
async def test_calculate_true_probability_with_and_without_charlie_confidence_booster():
    # deterministic path away from threshold
    engine_det = make_engine(binance=StubBinanceCurrent(Decimal("100000")))
    p1, c1 = await engine_det._calculate_true_probability(
        btc_price=Decimal("97000"),
        threshold=Decimal("96000"),
        direction="ABOVE",
        time_left=300,
    )
    assert p1 == Decimal("0.95")
    assert c1 == Decimal("0.95")

    class StubBooster:
        def apply_boost(self, _base_confidence, _direction):
            return Decimal("0.99")

    charlie = MagicMock()
    charlie.predict_15min_move = AsyncMock(return_value={"probability": "0.77", "confidence": "0.88"})
    engine_charlie = make_engine(charlie=charlie)
    engine_charlie.confidence_booster = StubBooster()

    p2, c2 = await engine_charlie._calculate_true_probability(
        btc_price=Decimal("95990"),
        threshold=Decimal("96000"),
        direction="ABOVE",
        time_left=300,
    )
    assert p2 == Decimal("0.77")
    assert c2 == Decimal("0.99")


def test_build_opportunity_and_parsers_misc_paths():
    engine = make_engine(config={"min_edge": "0.03", "max_edge": "0.5"})

    market = {"id": "m-build", "question": "Will BTC be above $96,000?"}

    yes_opp = engine._build_opportunity(
        market=market,
        yes_token_id="y",
        no_token_id="n",
        yes_price=Decimal("0.45"),
        no_price=Decimal("0.55"),
        true_prob=Decimal("0.75"),
        yes_edge=Decimal("0.30"),
        no_edge=Decimal("-0.10"),
        charlie_confidence=Decimal("0.8"),
        btc_price=Decimal("97000"),
        threshold=Decimal("96000"),
        direction="ABOVE",
        time_left=300,
    )
    assert yes_opp is not None
    assert yes_opp["side"] == "YES"

    no_opp = engine._build_opportunity(
        market=market,
        yes_token_id="y",
        no_token_id="n",
        yes_price=Decimal("0.95"),
        no_price=Decimal("0.10"),
        true_prob=Decimal("0.05"),
        yes_edge=Decimal("-0.20"),
        no_edge=Decimal("0.40"),
        charlie_confidence=Decimal("0.8"),
        btc_price=Decimal("95000"),
        threshold=Decimal("96000"),
        direction="BELOW",
        time_left=300,
    )
    assert no_opp is not None
    assert no_opp["side"] == "NO"

    assert engine._build_opportunity(
        market=market,
        yes_token_id="y",
        no_token_id="n",
        yes_price=Decimal("0.95"),
        no_price=Decimal("0.10"),
        true_prob=Decimal("0.05"),
        yes_edge=Decimal("1.00"),
        no_edge=Decimal("0.40"),
        charlie_confidence=Decimal("0.8"),
        btc_price=Decimal("95000"),
        threshold=Decimal("96000"),
        direction="BELOW",
        time_left=300,
    ) is None

    assert engine._extract_threshold_and_direction("Will BTC be above $96,000?") == (Decimal("96000"), "ABOVE")
    assert engine._extract_threshold_and_direction("Will BTC be below $95,000?") == (Decimal("95000"), "BELOW")
    assert engine._extract_threshold_and_direction("Will BTC hit $95,000?") == (Decimal("95000"), None)

    assert engine._extract_threshold("BTC above $96K") == Decimal("96000")
    assert engine._parse_start_price("Starts at $96,200") is None
    assert engine._parse_start_price("starting at $96,200") == Decimal("96200")


def test_time_and_orderbook_parsers_misc():
    engine = make_engine()

    soon = datetime.now(timezone.utc) + timedelta(seconds=40)
    assert engine._extract_time_left_seconds({"endDate": soon.isoformat().replace("+00:00", "Z")}) > 0
    assert engine._extract_time_left_seconds({"end_time": int((datetime.now(timezone.utc) + timedelta(seconds=50)).timestamp())}) is None
    with pytest.raises(InvalidOperation):
        engine._extract_time_left_seconds({"endDate": "invalid-date"})

    assert engine._extract_best_ask(None) is None
    assert engine._extract_best_ask({"asks": [["0.50", "10"], {"price": "0.49"}, {"bad": "x"}]}) == Decimal("0.49")
    assert engine._extract_mid_price({"bids": [{"price": "0.40"}], "asks": [{"price": "0.60"}]}) == Decimal("0.50")
    assert engine._extract_mid_price({"bids": [], "asks": []}) is None


@pytest.mark.asyncio
async def test_interval_start_price_and_start_price_parsers():
    polymarket = StubPolymarket()
    engine = make_engine(polymarket=polymarket, binance=StubBinanceCurrent(Decimal("96500")))

    market_desc = {"description": "starting at $95,500"}
    assert engine._get_market_start_price(market_desc) == Decimal("95500")

    market_meta = {"metadata": {"startPrice": "95400"}}
    assert engine._get_market_start_price(market_meta) == Decimal("95400")

    market_fallback = {"id": "m-fallback"}
    assert engine._get_market_start_price(market_fallback) == Decimal("96500")

    recent_start = {"startDate": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat().replace("+00:00", "Z")}
    assert await engine._get_interval_start_price(recent_start, Decimal("96600")) == Decimal("96600")

    old_start = {"startDate": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")}
    assert await engine._get_interval_start_price(old_start, Decimal("96600")) is None


@pytest.mark.asyncio
async def test_get_charlie_prediction_fallbacks():
    engine_none = make_engine(charlie=None)
    result_none = await engine_none._get_charlie_prediction(current_price=Decimal("1"), threshold=Decimal("1"), time_horizon=1)
    assert result_none["probability"] == Decimal("0.5")

    class NoPredict:
        pass

    engine_no_predict = make_engine(charlie=NoPredict())
    result_no_predict = await engine_no_predict._get_charlie_prediction(current_price=Decimal("1"), threshold=Decimal("1"), time_horizon=1)
    assert result_no_predict["probability"] == Decimal("0.5")

    class PredictNotDict:
        def predict_15min_move(self, **_kwargs):
            return "bad"

    engine_bad = make_engine(charlie=PredictNotDict())
    result_bad = await engine_bad._get_charlie_prediction(current_price=Decimal("1"), threshold=Decimal("1"), time_horizon=1)
    assert result_bad["probability"] == Decimal("0.5")
