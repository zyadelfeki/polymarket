"""Integration tests for Copilot patches.

Covers:
- Execution service contract
- Market lookup fallback contract
- YES/NO token direction correctness
- Rate limiting behavior
- Defensive imports for missing SDKs
"""

import asyncio
import importlib
import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide
from services.execution_service_v2 import ExecutionServiceV2, OrderResultDict
from strategies.latency_arbitrage import LatencyArbitrageEngine


class StubLedger:
    async def record_trade_entry(self, **kwargs):
        return "position_1"

    async def get_equity(self):
        return Decimal("1000")


class StubClient:
    paper_trading = True

    async def place_order(self, **kwargs):
        return {"success": True, "order_id": "order_123"}


@pytest.mark.asyncio
async def test_order_execution_contract_returns_dict():
    ledger = StubLedger()
    client = StubClient()
    service = ExecutionServiceV2(polymarket_client=client, ledger=ledger)

    result = await service.place_order(
        strategy="test",
        market_id="market_1",
        token_id="yes_token",
        side=OrderSide.BUY,
        quantity=Decimal("10"),
        price=Decimal("0.55"),
    )

    assert isinstance(result, dict)
    assert "success" in result
    assert "order_id" in result
    assert isinstance(result["success"], bool)
    assert isinstance(result["order_id"], (str, type(None)))

    # Ensure OrderSide enum is accepted without enum/string mismatches
    assert result.success is True


@pytest.mark.asyncio
async def test_market_lookup_gamma_fallback(monkeypatch):
    client = PolymarketClientV2(paper_trading=False)
    client.paper_trading = False

    class DummyClob:
        def get_market(self, market_id):
            return None

    client.client = DummyClob()

    gamma_called = {"called": False}

    async def fake_gamma(condition_id: str):
        gamma_called["called"] = True
        return {
            "condition_id": condition_id,
            "question": "Test market",
            "tokens": [
                {"token_id": "yes_token", "outcome": "YES", "price": 0.61},
                {"token_id": "no_token", "outcome": "NO", "price": 0.39},
            ],
        }

    async def no_throttle():
        return None

    async def no_best_ask(token_id):
        return None

    monkeypatch.setattr(client, "_fetch_market_via_gamma", fake_gamma)
    monkeypatch.setattr(client, "_throttle", no_throttle)
    monkeypatch.setattr(client, "_get_best_ask", no_best_ask)

    market = await client.get_market("cond_123")

    assert gamma_called["called"] is True
    assert isinstance(market, dict)
    assert "yes_price" in market
    assert "no_price" in market
    assert "yes_token_id" in market
    assert "no_token_id" in market


class CaptureExecutionService:
    def __init__(self):
        self.calls = []

    async def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return OrderResultDict(
            {
                "success": True,
                "order_id": "order_1",
                "status": None,
                "filled_quantity": Decimal("1"),
                "filled_price": Decimal("0.5"),
                "fees": Decimal("0"),
                "fills": [],
                "error": None,
                "slippage_bps": 0.0,
                "execution_time_ms": 0.0,
            }
        )


class StubCircuitBreaker:
    def __init__(self):
        self.state = SimpleNamespace(value="CLOSED")

    async def can_trade(self, equity):
        return True

    async def record_trade_result(self, new_equity, pnl):
        return None


@pytest.mark.asyncio
async def test_token_direction_yes_uses_yes_token():
    ledger = StubLedger()
    execution = CaptureExecutionService()
    circuit_breaker = StubCircuitBreaker()

    engine = LatencyArbitrageEngine(
        ledger=ledger,
        polymarket_client=object(),
        execution_service=execution,
        circuit_breaker=circuit_breaker,
        config={"max_position_pct": 10},
    )

    engine.yes_token_id = "yes_token"
    engine.no_token_id = "no_token"

    signal = {
        "action": "BUY_YES",
        "side": "YES",
        "spread_bps": 100,
        "target_price": Decimal("0.5"),
        "implied_probability": Decimal("0.6"),
        "polymarket_odds": Decimal("0.5"),
        "btc_price": Decimal("100000"),
        "confidence": 1.0,
    }

    await engine._execute_signal(signal)

    assert execution.calls, "Expected place_order to be called"
    call = execution.calls[-1]
    assert call["token_id"] == "yes_token"
    assert call["side"] == "BUY"


@pytest.mark.asyncio
async def test_token_direction_no_uses_no_token():
    ledger = StubLedger()
    execution = CaptureExecutionService()
    circuit_breaker = StubCircuitBreaker()

    engine = LatencyArbitrageEngine(
        ledger=ledger,
        polymarket_client=object(),
        execution_service=execution,
        circuit_breaker=circuit_breaker,
        config={"max_position_pct": 10},
    )

    engine.yes_token_id = "yes_token"
    engine.no_token_id = "no_token"

    signal = {
        "action": "BUY_NO",
        "side": "NO",
        "spread_bps": -100,
        "target_price": Decimal("0.5"),
        "implied_probability": Decimal("0.4"),
        "polymarket_odds": Decimal("0.5"),
        "btc_price": Decimal("100000"),
        "confidence": 1.0,
    }

    await engine._execute_signal(signal)

    assert execution.calls, "Expected place_order to be called"
    call = execution.calls[-1]
    assert call["token_id"] == "no_token"
    assert call["token_id"] != "yes_token"
    assert call["side"] == "BUY"


@pytest.mark.asyncio
async def test_rate_limiter_throttles():
    client = PolymarketClientV2(rate_limit=5.0, paper_trading=True)

    start = time.perf_counter()
    for _ in range(5):
        await client.place_order(
            token_id="yes",
            side=OrderSide.BUY,
            price=0.5,
            size=1.0,
            order_type="GTC",
            market_id="m1",
        )
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.5


@pytest.mark.asyncio
async def test_defensive_imports_missing_sdks(monkeypatch):
    modules_to_remove = [
        "web3",
        "py_clob_client",
        "py_clob_client.client",
        "py_clob_client.clob_types",
        "py_clob_client.order_builder.constants",
        "eth_account",
    ]

    for mod in modules_to_remove:
        monkeypatch.delitem(sys.modules, mod, raising=False)

    import data_feeds.polymarket_client_v2 as client_module

    client_module = importlib.reload(client_module)

    client = client_module.PolymarketClientV2(paper_trading=False)

    result = await client.place_order(
        token_id="yes",
        side=OrderSide.BUY,
        price=0.5,
        size=1.0,
        order_type="GTC",
        market_id="m1",
    )

    assert client_module.POLYMARKET_AVAILABLE is False
    assert isinstance(result, dict)
    assert result["success"] is False
    assert "error" in result
