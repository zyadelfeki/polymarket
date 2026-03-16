from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from main import TradingSystem
from models.external_signals import AdmissionVerdict
from services.execution_service_v2 import OrderResult, OrderStatus


class _StaticRecommendation:
    def __init__(self, side: str) -> None:
        self.side = side
        self.size = Decimal("10")
        self.kelly_fraction = Decimal("0.1")
        self.p_win = 0.62
        self.p_win_raw = 0.64
        self.p_win_calibrated = 0.62
        self.implied_prob = 0.50
        self.edge = 0.08
        self.confidence = 0.8
        self.regime = "BULLISH"
        self.technical_regime = "TRENDING"
        self.reason = "unit_test_charlie"
        self.model_votes = None
        self.ofi_conflict = False


class _StaticCharlieGate:
    def __init__(self, side: str) -> None:
        self._recommendation = _StaticRecommendation(side)

    async def evaluate_market(self, **kwargs):
        return self._recommendation


class _StaticAdmissionGate:
    def __init__(self, verdict: AdmissionVerdict) -> None:
        self._verdict = verdict

    async def evaluate(self, **kwargs):
        return self._verdict


def _build_config() -> dict:
    return {
        "trading": {
            "paper_trading": True,
            "min_price": 0.01,
            "max_price": 0.99,
            "max_position_size_pct": 10.0,
            "min_position_size": 10.0,
            "max_order_size": 1000.0,
        },
        "strategies": {
            "latency_arb": {
                "max_position_size_pct": 5.0,
            }
        },
        "startup": {
            "strategy_scan_min_interval_seconds": 0.0,
            "strategy_scan_timeout_seconds": 2.0,
            "network_timeout_seconds": 2.0,
        },
    }


@pytest.mark.asyncio
async def test_execute_opportunity_submits_order_when_valid():
    system = TradingSystem(_build_config())
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.circuit_breaker = AsyncMock()
    system.charlie_gate = _StaticCharlieGate("YES")

    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker.can_trade = AsyncMock(return_value=True)
    system.execution.place_order_with_risk_check = AsyncMock(
        return_value=OrderResult(
            success=True,
            order_id="ord-1",
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("10"),
            filled_price=Decimal("0.50"),
            fees=Decimal("0.01"),
        )
    )

    opportunity = {
        "market_id": "market-1",
        "token_id": "token-yes",
        "side": "YES",
        "edge": Decimal("0.05"),
        "market_price": Decimal("0.50"),
        "confidence": "HIGH",
        "direction": "UP",
    }

    await system._execute_opportunity(opportunity=opportunity, trigger="test")

    assert system.execution.place_order_with_risk_check.await_count == 1
    kwargs = system.execution.place_order_with_risk_check.await_args.kwargs
    assert kwargs["market_id"] == "market-1"
    assert kwargs["token_id"] == "token-yes"
    assert kwargs["side"] == "BUY"
    assert kwargs["price"] == Decimal("0.50")
    assert kwargs["quantity"] > Decimal("0")


@pytest.mark.asyncio
async def test_execute_opportunity_skips_when_circuit_breaker_blocks():
    system = TradingSystem(_build_config())
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.circuit_breaker = AsyncMock()
    system.charlie_gate = _StaticCharlieGate("YES")

    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker.can_trade = AsyncMock(return_value=False)

    opportunity = {
        "market_id": "market-1",
        "token_id": "token-yes",
        "side": "YES",
        "edge": Decimal("0.05"),
        "market_price": Decimal("0.50"),
        "confidence": "MEDIUM",
    }

    await system._execute_opportunity(opportunity=opportunity, trigger="test")

    assert system.execution.place_order_with_risk_check.await_count == 0


@pytest.mark.asyncio
async def test_execute_opportunity_skips_when_external_admission_blocks():
    system = TradingSystem(_build_config())
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.circuit_breaker = AsyncMock()
    system.charlie_gate = _StaticCharlieGate("YES")
    system.pre_trade_admission = _StaticAdmissionGate(
        AdmissionVerdict(
            allowed=False,
            confidence_multiplier=Decimal("0"),
            size_multiplier=Decimal("0"),
            block_reason="provider_quorum_failed",
            health_flags=["coingecko_stale"],
        )
    )

    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker.can_trade = AsyncMock(return_value=True)

    opportunity = {
        "market_id": "market-1",
        "token_id": "token-yes",
        "side": "YES",
        "edge": Decimal("0.05"),
        "market_price": Decimal("0.50"),
        "confidence": "HIGH",
        "direction": "UP",
        "btc_price": Decimal("90000"),
    }

    await system._execute_opportunity(opportunity=opportunity, trigger="test")

    assert system.execution.place_order_with_risk_check.await_count == 0


@pytest.mark.asyncio
async def test_run_strategy_scan_executes_found_opportunity():
    system = TradingSystem(_build_config())
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.circuit_breaker = AsyncMock()
    system.strategy_engine = AsyncMock()
    system.charlie_gate = _StaticCharlieGate("NO")

    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker.can_trade = AsyncMock(return_value=True)

    opportunity = {
        "market_id": "market-1",
        "token_id": "token-no",
        "side": "NO",
        "edge": Decimal("0.04"),
        "market_price": Decimal("0.40"),
        "confidence": "HIGH",
        "direction": "DOWN",
    }
    system.strategy_engine.scan_opportunities = AsyncMock(return_value=opportunity)

    system.execution.place_order_with_risk_check = AsyncMock(
        return_value=OrderResult(
            success=True,
            order_id="ord-2",
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("12.5"),
            filled_price=Decimal("0.40"),
            fees=Decimal("0.01"),
        )
    )

    await system._run_strategy_scan(trigger="unit_test")

    assert system.strategy_engine.scan_opportunities.await_count == 1
    assert system.execution.place_order_with_risk_check.await_count == 1
