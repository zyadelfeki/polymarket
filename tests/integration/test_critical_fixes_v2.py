import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from services.network_health import NetworkPartitionError
from risk.circuit_breaker_v2 import CircuitBreakerV2, CircuitState
from strategies.latency_arbitrage import LatencyArbitrageEngine
from database.ledger_async import AsyncLedger
from main_v2 import TradingBot


class StubAlertService:
    def __init__(self):
        self.sent = []

    async def send_critical_alert(self, title: str, message: str):
        self.sent.append((title, message))


@pytest.mark.asyncio
async def test_auth_retry_and_shutdown(tmp_path):
    client = PolymarketClientV2(paper_trading=True, retry_backoff_base=0)
    client.max_retries = 3
    client.max_auth_retries = 3

    alert = StubAlertService()
    circuit_breaker = CircuitBreakerV2(initial_equity=Decimal("1000"), alert_service=alert)

    async def handler(reason: str) -> None:
        await circuit_breaker.manual_trip(reason)

    client.set_auth_failure_handler(handler)

    class AuthError(Exception):
        status_code = 401

    async def fail_call():
        raise AuthError("unauthorized")

    result = await client._execute_with_retries("auth_test", fail_call)
    assert result is None
    assert client.emergency_shutdown_reason == "AUTH_FAILURE_CRITICAL"
    assert client.can_trade is False
    assert circuit_breaker.state == CircuitState.OPEN
    assert alert.sent


@pytest.mark.asyncio
async def test_position_reconciliation_imports_orphaned(tmp_path):
    db_path = tmp_path / "positions.db"
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()

    class StubClient:
        async def get_open_positions(self):
            return [
                {
                    "token_id": "token_1",
                    "market_id": "m1",
                    "quantity": "1",
                    "price": "0.5",
                    "side": "BUY",
                }
            ]

    bot = TradingBot(config={"mode": "paper", "initial_capital": 10})
    bot.ledger = ledger
    bot.polymarket_client = StubClient()

    await bot._reconcile_positions_on_startup()
    positions = await ledger.get_open_positions()
    assert len(positions) == 1
    assert positions[0].token_id == "token_1"

    await ledger.close()


@pytest.mark.asyncio
async def test_network_partition_blocks_order_v2():
    class StubClient:
        paper_trading = True

    class StubLedger:
        async def get_idempotency_record(self, *_):
            return None

    service = ExecutionServiceV2(StubClient(), StubLedger())
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
    assert result.error_code == "network_partition"


@pytest.mark.asyncio
async def test_slippage_violation_v2():
    class SlippageClient:
        paper_trading = False

        async def place_order(self, **_):
            return {"success": True, "order_id": "order_1"}

        async def get_order_status(self, _):
            return {"fills": [{"id": "f1", "size": "10", "price": "0.75", "fee": "0"}]}

        async def cancel_order(self, _):
            return True

    class StubLedger:
        async def record_audit_event(self, **_):
            return None

        async def get_idempotency_record(self, *_):
            return None

        async def record_idempotency(self, *_ , **__):
            return None

        async def update_idempotency(self, *_ , **__):
            return None

    service = ExecutionServiceV2(SlippageClient(), StubLedger())

    result = await service.place_order(
        strategy="test",
        market_id="0x" + "b" * 64,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        max_slippage_bps=50,
    )

    assert result.success is False
    assert result.error_code == "slippage_violation"


@pytest.mark.asyncio
async def test_circuit_breaker_alerts_on_trip():
    alert = StubAlertService()
    breaker = CircuitBreakerV2(initial_equity=Decimal("1000"), alert_service=alert)
    await breaker.manual_trip("manual")
    assert alert.sent


def test_strategy_health_pause_trigger():
    class StubLedger:
        def calculate_breakeven_price(self, entry_price, quantity, fee_rate=Decimal("0.02")):
            return entry_price

    class StubClient:
        pass

    class StubExecution:
        pass

    class StubBreaker:
        pass

    strategy = LatencyArbitrageEngine(
        ledger=StubLedger(),
        polymarket_client=StubClient(),
        execution_service=StubExecution(),
        circuit_breaker=StubBreaker(),
    )

    for _ in range(40):
        strategy.record_trade_outcome(win=False, roi=Decimal("-0.05"))

    healthy, _ = strategy._evaluate_strategy_health()
    assert healthy is False
    strategy._pause_strategy(60)
    assert strategy._is_paused() is True


@pytest.mark.asyncio
async def test_market_resolution_monitor_paths():
    class StubLedger:
        def __init__(self):
            self.closed = []

        async def get_open_positions(self):
            return [
                SimpleNamespace(
                    id=1,
                    market_id="m1",
                    token_id="t1",
                    strategy="latency",
                    entry_price=Decimal("0.50"),
                    quantity=Decimal("10"),
                )
            ]

        async def record_trade_exit(self, **kwargs):
            self.closed.append(kwargs)

    class StubExecution:
        def __init__(self):
            self.calls = 0

        async def close_position(self, **_):
            self.calls += 1
            return SimpleNamespace(success=True, filled_price=Decimal("0.55"))

    class StubClient:
        def __init__(self):
            self.calls = 0

        async def get_market(self, market_id: str):
            self.calls += 1
            if self.calls == 1:
                return {
                    "status": "ACTIVE",
                    "end_date": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                }
            return {"status": "RESOLVED"}

    bot = TradingBot(config={"market_monitor_interval": 0.1})
    bot.running = True
    bot.ledger = StubLedger()
    bot.execution_service = StubExecution()
    bot.polymarket_client = StubClient()
    bot.strategy = None

    task = asyncio.create_task(bot._market_resolution_monitor())
    await asyncio.sleep(0.3)
    bot.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert bot.execution_service.calls >= 1
    assert bot.ledger.closed


def test_transaction_cost_prediction_breakeven():
    class StubLedger:
        @staticmethod
        def calculate_breakeven_price(entry_price, quantity, fee_rate=Decimal("0.02")):
            return entry_price * (Decimal("1") + fee_rate) / (Decimal("1") - fee_rate)

    strategy = LatencyArbitrageEngine(
        ledger=StubLedger(),
        polymarket_client=SimpleNamespace(),
        execution_service=SimpleNamespace(),
        circuit_breaker=SimpleNamespace(),
        config={"fee_rate": 0.02, "min_profit_buffer_pct": 0.05},
    )

    breakeven = strategy._calculate_breakeven_with_costs(
        entry_price=Decimal("0.50"),
        quantity=Decimal("10"),
        spread=Decimal("0.02"),
    )
    assert breakeven > Decimal("0.50")
