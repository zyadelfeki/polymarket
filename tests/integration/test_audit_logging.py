from decimal import Decimal
import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from database.ledger_async import AsyncLedger
from risk.circuit_breaker_v2 import CircuitBreakerV2


VALID_MARKET_ID = "0x" + "b" * 64


@pytest.mark.asyncio
async def test_order_and_position_audit_logs(tmp_path):
    db_path = tmp_path / "audit.db"
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()

    client = PolymarketClientV2(paper_trading=True)
    service = ExecutionServiceV2(client, ledger)

    result = await service.place_order(
        strategy="audit_test",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.55"),
    )

    order_rows = await ledger.execute(
        "SELECT old_state, new_state FROM audit_log WHERE entity_type='order' AND entity_id=?",
        (result["order_id"],),
        fetch_all=True,
    )
    assert order_rows, "Expected order audit records"

    states = {row[1] for row in order_rows}
    assert "pending" in states or "PENDING" in states

    position_rows = await ledger.execute(
        "SELECT new_state FROM audit_log WHERE entity_type='position'",
        fetch_all=True,
    )
    assert position_rows, "Expected position audit records"

    await ledger.close()

@pytest.mark.asyncio
async def test_circuit_breaker_audit_logs(tmp_path):
    db_path = tmp_path / "audit_cb.db"
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()

    breaker = CircuitBreakerV2(
        initial_equity=Decimal("1000"),
        audit_logger=ledger,
    )

    await breaker.manual_trip("manual test")
    await breaker.manual_reset()

    cb_rows = await ledger.execute(
        "SELECT new_state FROM audit_log WHERE entity_type='circuit_breaker'",
        fetch_all=True,
    )
    assert cb_rows, "Expected circuit breaker audit records"

    await ledger.close()
