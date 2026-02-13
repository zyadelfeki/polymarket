import pytest
from decimal import Decimal

from services.execution_service_v2 import ExecutionServiceV2


class StubClient:
    def __init__(self, response):
        self.response = response
        self.paper_trading = True

    async def get_wallet_balance(self):
        return self.response


class StubLedger:
    async def record_trade_entry(self, **kwargs):
        return "position_1"


@pytest.mark.asyncio
async def test_balance_fetch_invalid_response_trips_circuit_breaker():
    client = StubClient(response={})
    service = ExecutionServiceV2(client, StubLedger())

    with pytest.raises(ValueError):
        await service.get_real_balance()

    assert service.circuit_breaker_active is True


@pytest.mark.asyncio
async def test_balance_fetch_zero_trips_circuit_breaker():
    client = StubClient(response={"balance": Decimal("0")})
    service = ExecutionServiceV2(client, StubLedger())

    with pytest.raises(ValueError):
        await service.get_real_balance()

    assert service.circuit_breaker_active is True
