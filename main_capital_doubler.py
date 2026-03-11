"""Compatibility module for legacy CapitalDoublerBot imports.

The original capital doubler entry point is not present in this branch, but a
small subset of its interface is still referenced by tests and paper-trading
helpers. This shim keeps those imports stable without coupling them to the much
heavier production runtime.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional


class CapitalDoublerBot:
    """Small compatibility bot used by legacy integration tests.

    The full production path is driven by ``main.py``. This adapter only
    preserves the older constructor and a few lifecycle methods that existing
    tests still exercise.
    """

    def __init__(
        self,
        *,
        starting_capital: Optional[Decimal] = None,
        initial_capital: Optional[Decimal] = None,
        mode: str = "paper",
        paper_trading: Optional[bool] = None,
        config: Optional[dict[str, Any]] = None,
    ) -> None:
        capital_value = starting_capital if starting_capital is not None else initial_capital
        if capital_value is None:
            capital_value = Decimal("0")

        self.capital = Decimal(str(capital_value))
        self.starting_capital = self.capital
        self.mode = mode
        self.paper_trading = paper_trading if paper_trading is not None else mode == "paper"
        self.config = config or {}
        self.is_running = False
        self.realized_losses = Decimal("0")
        self.max_drawdown_pct = Decimal(str(self.config.get("max_drawdown_pct", "10.0")))
        self.price_subscriber = None
        self.intelligence_subscriber = None

    def set_ipc_subscribers(self, price_subscriber: Any, intelligence_subscriber: Any) -> None:
        self.price_subscriber = price_subscriber
        self.intelligence_subscriber = intelligence_subscriber

    async def scan_once(self) -> Optional[dict[str, Any]]:
        return None

    def record_loss(self, amount: Decimal) -> None:
        loss = Decimal(str(amount))
        if loss < 0:
            raise ValueError("loss amount must be non-negative")
        self.realized_losses += loss
        self.capital -= loss

    async def check_circuit_breaker(self) -> None:
        if self.starting_capital <= 0:
            return

        drawdown_pct = (self.realized_losses / self.starting_capital) * Decimal("100")
        if drawdown_pct >= self.max_drawdown_pct:
            raise RuntimeError(
                f"Circuit breaker triggered at {drawdown_pct.quantize(Decimal('0.01'))}% drawdown"
            )


__all__ = ["CapitalDoublerBot"]