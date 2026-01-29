import os
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.execution_service_v2 import ExecutionServiceV2
from services.execution_service import ExecutionService as LegacyExecutionService
from services.error_codes import ErrorCode
from services.network_health import NetworkHealthMonitor
from services.strategy_health import StrategyHealthMonitor
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from data_feeds.binance_websocket import BinanceWebSocketV2
from database.ledger import Ledger
from risk.circuit_breaker_v2 import CircuitBreakerV2


class StubLedger:
    async def record_trade_entry(self, **kwargs):
        return "position_1"

    async def get_equity(self):
        return Decimal("1000.00")


class SlippageStubClient:
    def __init__(self):
        self.paper_trading = False

    async def place_order(self, **kwargs):
        return {"success": True, "order_id": "order_1"}

    async def get_order_status(self, order_id: str):
        return {
            "fills": [
                {"id": "fill_1", "size": "10", "price": "0.75", "fee": "0.01"}
            ]
        }

    async def cancel_order(self, order_id: str):
        return True


class StubAlertService:
    def __init__(self):
        self.sent = []

    async def send_critical_alert(self, title: str, message: str):
        self.sent.append((title, message))


@pytest.mark.asyncio
async def test_network_partition_blocks_order():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()
    service = ExecutionServiceV2(client, ledger)

    service.network_monitor.state.last_successful_api_call = datetime.utcnow() - timedelta(seconds=60)
    result = await service.place_order(
        strategy="test",
        market_id="0x" + "a" * 64,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
    )

    assert result.success is False
    assert result.error_code == ErrorCode.NETWORK_PARTITION.value


@pytest.mark.asyncio
async def test_slippage_violation_returns_error():
    client = SlippageStubClient()
    ledger = StubLedger()
    service = ExecutionServiceV2(client, ledger)

    result = await service.place_order(
        strategy="test",
        market_id="0x" + "a" * 64,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        max_slippage_bps=50,
    )

    assert result.success is False
    assert result.error_code == ErrorCode.SLIPPAGE_VIOLATION.value


@pytest.mark.asyncio
async def test_auth_retry_triggers_shutdown():
    client = PolymarketClientV2(paper_trading=True)
    client.max_auth_retries = 1
    client.auth_retry_count = 0

    result = await client._handle_auth_error(401)
    assert result is False
    assert client.can_trade is False
    assert client.emergency_shutdown_reason == "AUTH_FAILURE_CRITICAL"


def test_breakeven_price_calculation():
    breakeven = Ledger.calculate_breakeven_price(Decimal("0.50"), Decimal("10"), Decimal("0.02"))
    assert breakeven.quantize(Decimal("0.0001")) == Decimal("0.5204")


def test_strategy_health_monitor_degrades():
    monitor = StrategyHealthMonitor("latency_arb", min_samples=5)
    for _ in range(5):
        monitor.record_trade(win=False, roi=Decimal("-0.05"))
    healthy, reason = monitor.check_health()
    assert healthy is False
    assert "Win rate" in reason or "Sharpe" in reason


@pytest.mark.asyncio
async def test_binance_rest_fallback_updates_prices():
    class TestWS(BinanceWebSocketV2):
        async def _fetch_rest_prices(self):
            return {"BTCUSDT": 100.0}

    feed = TestWS()
    feed.running = True
    feed.websocket = type("ws", (), {"closed": True})()

    task = asyncio.create_task(feed._price_fallback_loop())
    await asyncio.sleep(0.2)
    feed.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert feed.fallback_active is True
    assert feed.get_current_price("BTC") == 100.0


@pytest.mark.asyncio
async def test_circuit_breaker_sends_alert():
    alert = StubAlertService()
    cb = CircuitBreakerV2(initial_equity=Decimal("1000"), alert_service=alert)

    await cb.manual_trip("manual")
    assert alert.sent


@pytest.mark.asyncio
async def test_position_reconciliation_imports_orphaned():
    class StubLedgerSync:
        def __init__(self):
            self.imported = False

        def get_open_positions(self):
            return []

        def record_trade_entry(self, **kwargs):
            self.imported = True
            return 1

        def record_reconciled_position(self, **kwargs):
            self.imported = True
            return 1

        def record_audit_event(self, **kwargs):
            return 1

    class StubClient:
        async def get_open_positions(self):
            return [{"token_id": "token_1", "market_id": "m1", "quantity": "1", "price": "0.5"}]

    from main_production import ProductionTradingBot

    bot = ProductionTradingBot.__new__(ProductionTradingBot)
    bot.ledger = StubLedgerSync()
    bot.polymarket_client = StubClient()

    await bot._reconcile_positions_on_startup()
    assert bot.ledger.imported is True


@pytest.mark.asyncio
async def test_market_resolution_monitor_handles_resolved():
    class StubLedgerSync:
        def get_open_positions(self):
            return [{"id": 1, "market_id": "m1"}]

        def record_audit_event(self, **kwargs):
            return 1

    class StubClient:
        async def get_market(self, market_id: str):
            return {"status": "RESOLVED"}

    from main_production import ProductionTradingBot

    bot = ProductionTradingBot.__new__(ProductionTradingBot)
    bot.running = True
    bot.ledger = StubLedgerSync()
    bot.polymarket_client = StubClient()
    bot._handle_resolved_position_called = False

    async def _handle_resolved_position(_):
        bot._handle_resolved_position_called = True
        bot.running = False

    bot._handle_resolved_position = _handle_resolved_position

    await asyncio.wait_for(bot._market_resolution_monitor(), timeout=2)
    assert bot._handle_resolved_position_called is True


@pytest.mark.asyncio
async def test_legacy_execution_slippage_violation():
    class LegacySlippageClient:
        async def place_order(self, **_):
            return {"success": True, "order_id": "legacy_order"}

        async def get_order_status(self, _):
            return {"status": "MATCHED", "filled_price": "0.75", "filled_quantity": "10", "fees": "0"}

        async def cancel_order(self, _):
            return True

    class LegacyLedger:
        def record_trade_entry(self, **_):
            return 1

        def get_equity(self):
            return Decimal("1000")

    service = LegacyExecutionService(LegacySlippageClient(), LegacyLedger())
    result = await service.place_order(
        strategy="legacy",
        market_id="m1",
        token_id="t1",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        max_slippage_bps=50,
    )

    assert result.success is False
    assert result.error == "slippage_violation"


@pytest.mark.asyncio
async def test_legacy_execution_blocks_on_partition():
    class LegacyClient:
        async def place_order(self, **_):
            return {"success": True, "order_id": "legacy_order"}

        async def get_order_status(self, _):
            return {"status": "MATCHED", "filled_price": "0.50", "filled_quantity": "10", "fees": "0"}

    class LegacyLedger:
        def record_trade_entry(self, **_):
            return 1

        def get_equity(self):
            return Decimal("1000")

    service = LegacyExecutionService(LegacyClient(), LegacyLedger())
    service.network_monitor.state.last_successful_api_call = datetime.utcnow() - timedelta(seconds=60)

    result = await service.place_order(
        strategy="legacy",
        market_id="m1",
        token_id="t1",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
    )

    assert result.success is False
    assert result.error == "network_partition"