import os
import sys
from decimal import Decimal
import asyncio

import pytest
import pytest_asyncio

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from database.ledger_async import AsyncLedger


VALID_MARKET_ID = "0x" + "d" * 64


@pytest_asyncio.fixture(autouse=True)
async def cleanup_after_test():
    """Cleanup async resources after each test to prevent hanging."""
    yield
    # Cancel all pending tasks
    current_task = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if not t.done() and t is not current_task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_idempotency_persists_across_restart(tmp_path):
    db_path = tmp_path / "trading.db"

    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()
    client = PolymarketClientV2(paper_trading=True)
    service = ExecutionServiceV2(client, ledger)

    result1 = await service.place_order(
        strategy="test",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="idem_key_1",
    )

    result2 = await service.place_order(
        strategy="test",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="idem_key_1",
    )

    assert result1["order_id"] == result2["order_id"]
    assert result2["is_duplicate"] is True

    record = await ledger.get_idempotency_record("idem_key_1")
    assert record is not None
    assert record["order_id"] == result1["order_id"]

    count = await ledger.execute_scalar("SELECT COUNT(*) FROM idempotency_log")
    assert int(count) == 1

    lines = await ledger.execute_scalar("SELECT COUNT(*) FROM transaction_lines")
    assert int(lines) == 2

    await ledger.close()

    ledger2 = AsyncLedger(db_path=str(db_path))
    await ledger2.initialize()
    service2 = ExecutionServiceV2(client, ledger2)

    result3 = await service2.place_order(
        strategy="test",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="idem_key_1",
    )

    assert result3["is_duplicate"] is True
    assert result3["order_id"] == result1["order_id"]

    await ledger2.close()
