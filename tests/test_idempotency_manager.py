from decimal import Decimal

from execution.idempotency_manager import IdempotencyManager


def test_idempotency_manager_deduplicates(tmp_path):
    db_path = tmp_path / "idempotency.json"
    manager = IdempotencyManager(db_path=str(db_path), ttl=3600)

    key = manager.generate_key(
        market_id="test_market",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.5"),
        strategy="test_strategy",
    )

    assert manager.check_duplicate(key) is None

    manager.record_order(key, {"order_id": "abc", "success": True})
    cached = manager.check_duplicate(key)

    assert cached is not None
    assert cached["order_id"] == "abc"
