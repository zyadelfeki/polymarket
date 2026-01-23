import os
import sys
from decimal import Decimal

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2, OrderRequest
from data_feeds.polymarket_client_v2 import OrderSide
from logs.precision_monitor import PrecisionMonitor, PrecisionError
from utils.decimal_json import dumps as decimal_dumps, loads as decimal_loads
from database.ledger_async import AsyncLedger


VALID_MARKET_ID = "0x" + "c" * 64


class StubLedger:
    async def record_trade_entry(self, **kwargs):
        return "position_1"

    async def get_equity(self):
        return Decimal("1000.00")


@pytest.mark.asyncio
async def test_decimal_addition_exact():
    assert Decimal(str(0.1)) + Decimal(str(0.2)) == Decimal("0.3")


@pytest.mark.asyncio
async def test_place_order_converts_to_decimal():
    client = PolymarketClientV2(paper_trading=True)
    service = ExecutionServiceV2(client, StubLedger())

    result = await service.place_order(
        strategy="test",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        side="BUY",
        quantity="10.1234",
        price="0.56789",
    )

    order_state = service.orders[result.order_id]
    assert isinstance(order_state.request.quantity, Decimal)
    assert isinstance(order_state.request.price, Decimal)
    assert order_state.request.quantity == Decimal("10.12")
    assert order_state.request.price == Decimal("0.5679")


def test_precision_monitor_raises_on_excess_decimals():
    monitor = PrecisionMonitor()
    with pytest.raises(PrecisionError):
        monitor.check_equity(Decimal("100.000000001"))


def test_order_request_rejects_float_inputs():
    with pytest.raises(TypeError):
        OrderRequest(
            strategy="test",
            market_id=VALID_MARKET_ID,
            token_id="yes",
            side=OrderSide.BUY,
            quantity=1.0,
            price=0.5,
        )


def test_precision_stability_over_many_trades():
    equity = Decimal("0.00")
    for _ in range(1000):
        equity += Decimal("0.01")
    assert abs(equity - Decimal("10.00")) <= Decimal("0.01")


def test_decimal_json_round_trip_stable():
    payload = {"value": Decimal("0.01")}
    for _ in range(5):
        payload = decimal_loads(decimal_dumps(payload))
    assert payload["value"] == Decimal("0.01")


@pytest.mark.asyncio
async def test_decimal_json_db_round_trip(tmp_path):
    db_path = tmp_path / "audit.db"
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()

    await ledger.record_audit_event(
        entity_type="test",
        entity_id="1",
        old_state=None,
        new_state="stored",
        reason="decimal_round_trip",
        context={"value": Decimal("0.01")},
        correlation_id="corr_decimal",
    )

    stored = await ledger.execute_scalar(
        "SELECT context FROM audit_log ORDER BY id DESC LIMIT 1"
    )
    decoded = decimal_loads(stored)
    assert decoded["value"] == Decimal("0.01")

    await ledger.close()
