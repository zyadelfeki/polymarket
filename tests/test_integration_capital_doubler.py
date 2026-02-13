"""
Integration test - Full system end-to-end
"""

import pytest
from decimal import Decimal

from main_capital_doubler import CapitalDoublerBot


@pytest.mark.asyncio
async def test_capital_doubler_initialization():
    """Verify bot initializes all components"""
    bot = CapitalDoublerBot(
        starting_capital=Decimal("10.00"),
        mode="paper",
        config={
            "min_edge": 0.03,
            "max_edge": 0.50,
            "scan_interval": 1.0,
        },
    )

    assert bot.capital == Decimal("10.00")
    assert bot.mode == "paper"
    assert bot.is_running is False


@pytest.mark.asyncio
async def test_capital_doubler_scan_cycle():
    """Verify one scan cycle completes without errors"""
    bot = CapitalDoublerBot(
        starting_capital=Decimal("10.00"),
        mode="paper",
    )

    opportunity = await bot.scan_once()

    assert opportunity is None or isinstance(opportunity, dict)


@pytest.mark.asyncio
async def test_capital_doubler_circuit_breaker():
    """Verify circuit breaker stops bot on losses"""
    bot = CapitalDoublerBot(
        starting_capital=Decimal("100.00"),
        mode="paper",
    )

    bot.record_loss(Decimal("15.00"))

    with pytest.raises(Exception) as exc:
        await bot.check_circuit_breaker()

    assert "circuit breaker" in str(exc.value).lower()
