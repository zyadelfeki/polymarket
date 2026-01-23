import os
import sys
from decimal import Decimal

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from database.ledger_async import AsyncLedger


VALID_MARKET_ID = "0x" + "e" * 64


@pytest.mark.asyncio
async def test_double_entry_accounting(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()

    await ledger.record_deposit(Decimal("1000.00"))

    position_id = await ledger.record_trade_entry(
        order_id="order_1",
        market_id=VALID_MARKET_ID,
        token_id="yes",
        strategy="test",
        side="BUY",
        quantity=Decimal("100"),
        price=Decimal("0.50"),
        correlation_id="corr_1",
    )

    cash_balance = await ledger.execute_scalar(
        "SELECT balance FROM accounts WHERE account_name='Cash'"
    )
    positions_balance = await ledger.execute_scalar(
        "SELECT balance FROM accounts WHERE account_name='Positions'"
    )

    assert Decimal(str(cash_balance)) == Decimal("950.00")
    assert Decimal(str(positions_balance)) == Decimal("50.00")

    txn_id = await ledger.execute_scalar(
        "SELECT id FROM transactions WHERE reference_id = ?",
        ("order_1",)
    )

    line_count = await ledger.execute_scalar(
        "SELECT COUNT(*) FROM transaction_lines WHERE transaction_id = ?",
        (txn_id,)
    )
    assert int(line_count) == 2

    audit_count = await ledger.execute_scalar(
        "SELECT COUNT(*) FROM audit_log WHERE entity_type = 'TRANSACTION' AND entity_id = ?",
        (txn_id,)
    )
    assert int(audit_count) == 2

    await ledger.close()
