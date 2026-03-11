"""
SUB-SECOND EXECUTION - The difference between 30% and 300% monthly returns
"""

from __future__ import annotations

import asyncio
import inspect
import time
from decimal import Decimal
from typing import Dict, Optional, Any

import logging

from config.settings import settings
from risk.kelly_sizer import AdaptiveKellySizer
from integrations.charlie_booster import CharliePredictionBooster
from utils.decimal_helpers import to_decimal, quantize_price, quantize_quantity, to_timeout_float
from execution.idempotency_manager import IdempotencyManager
from execution.order_types import OrderResult

logger = logging.getLogger(__name__)


def _decimal_from_runtime(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class UltraFastExecutor:
    def __init__(
        self,
        execution_service,
        ledger,
        kelly_sizer: Optional[AdaptiveKellySizer] = None,
        charlie_booster: Optional[CharliePredictionBooster] = None,
        config: Optional[Dict] = None,
    ) -> None:
        self.execution = execution_service
        self.ledger = ledger
        self.idempotency = IdempotencyManager(db_path=":memory:", cache_ttl=300)
        max_aggregate_exposure = Decimal(str(getattr(settings, "MAX_AGGREGATE_EXPOSURE", "20.0")))
        self.kelly = kelly_sizer or AdaptiveKellySizer(
            config={
                "kelly_fraction": "0.25",
                "max_bet_pct": settings.MAX_POSITION_SIZE_PCT,
                "min_edge": settings.MIN_EDGE_THRESHOLD,
                "max_aggregate_exposure": max_aggregate_exposure,
                "min_bet_size": Decimal(str(settings.MIN_BET_SIZE)),
                "micro_capital_threshold": Decimal(str(settings.MICRO_CAPITAL_THRESHOLD)),
            }
        )
        self.charlie = charlie_booster

        cfg = config or {}
        self.order_timeout_seconds = Decimal(str(cfg.get("order_timeout_seconds", "0.5")))
        self.limit_price_buffer = Decimal(str(cfg.get("limit_price_buffer", "0.01")))
        default_trade_pct = Decimal(str(settings.MAX_POSITION_SIZE_PCT)) / Decimal("100")
        self.max_trade_pct = Decimal(str(cfg.get("max_trade_pct", default_trade_pct)))

    async def execute_trade(
        self,
        opportunity: Dict,
        capital: Decimal,
        bet_size: Optional[Decimal] = None,
    ) -> Optional[OrderResult]:
        if bet_size is None:
            bet_size = await self.calculate_bet_size(opportunity, capital)

        if bet_size <= 0:
            return None

        market_price = quantize_price(_decimal_from_runtime(opportunity["market_price"]))
        limit_price = quantize_price(market_price * (Decimal("1") + self.limit_price_buffer))
        quantity = quantize_quantity(bet_size / limit_price)

        if quantity <= 0:
            return None

        try:
            order = await asyncio.wait_for(
                self.execute_order(
                    market_id=opportunity["market_id"],
                    outcome=opportunity.get("side"),
                    side="BUY",
                    price=limit_price,
                    size=quantity,
                    token_id=opportunity["token_id"],
                    strategy="latency_arbitrage_btc",
                    metadata={
                        "side": opportunity["side"],
                        "edge": str(opportunity["edge"]),
                        "true_prob": str(opportunity["true_prob"]),
                        "market_price": str(opportunity["market_price"]),
                    },
                ),
                timeout=to_timeout_float(self.order_timeout_seconds),
            )
            return dict(order) if isinstance(order, dict) else order
        except asyncio.TimeoutError:
            logger.error("Execution too slow - opportunity missed")
            return None

    async def execute_order(
        self,
        market_id: str,
        outcome: Optional[str],
        side: str,
        price: Decimal,
        size: Decimal,
        token_id: Optional[str] = None,
        strategy: str = "ultra_fast_executor",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> OrderResult:
        """
        Execute order with idempotency protection.
        Returns cached result if duplicate detected.
        """
        token_id = token_id or outcome or "unknown_token"
        idem_key = self.idempotency.generate_key(
            market_id=market_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
        )

        if self.idempotency.is_duplicate(idem_key):
            cached = self.idempotency.get_cached_result(idem_key)
            if cached:
                logger.warning(
                    "returning_cached_order_result | key=%s order_id=%s",
                    idem_key,
                    cached.get("order_id"),
                )
                return cached
            return {
                "success": False,
                "order_id": None,
                "error": "Duplicate order prevented (no cached result)",
                "filled_size": None,
                "avg_price": None,
                "timestamp": time.time(),
            }

        logger.info(
            "executing_new_order | key=%s market=%s outcome=%s side=%s",
            idem_key,
            market_id,
            outcome,
            side,
        )

        self.idempotency.record(idem_key, status="pending")

        try:
            result = await self.execution.place_order(
                strategy=strategy,
                market_id=market_id,
                token_id=token_id,
                side=side,
                quantity=size,
                price=price,
                metadata=metadata,
                max_slippage_bps=100,
            )
        except Exception as exc:
            error_result: OrderResult = {
                "success": False,
                "order_id": None,
                "error": str(exc),
                "filled_size": None,
                "avg_price": None,
                "timestamp": time.time(),
            }
            self.idempotency.update_result(idem_key, error_result)
            return error_result

        if isinstance(result, dict):
            self.idempotency.update_result(idem_key, result)
        else:
            self.idempotency.update_result(idem_key, {"success": False, "error": "invalid_result"})

        return result if isinstance(result, dict) else {
            "success": False,
            "order_id": None,
            "error": "invalid_result",
            "filled_size": None,
            "avg_price": None,
            "timestamp": time.time(),
        }

    async def calculate_bet_size(self, opportunity: Dict, capital: Decimal) -> Decimal:
        bankroll = _decimal_from_runtime(capital)
        live_equity_getter = getattr(self.ledger, "get_equity", None)
        if callable(live_equity_getter):
            # Sizing must use the live ledger equity, not a stale startup snapshot.
            try:
                live_equity = live_equity_getter()
                if inspect.isawaitable(live_equity):
                    live_equity = await live_equity
                bankroll = _decimal_from_runtime(live_equity)
            except Exception:
                pass

        edge = _decimal_from_runtime(opportunity.get("edge", "0"))
        market_price = _decimal_from_runtime(opportunity.get("market_price"))
        true_prob = _decimal_from_runtime(opportunity.get("true_prob", "0.5"))

        win_probability = true_prob if opportunity.get("side") == "YES" else (Decimal("1") - true_prob)
        payout_odds = Decimal("1") / market_price

        exposure = await self._current_aggregate_exposure()
        result = self.kelly.calculate_bet_size(
            bankroll=bankroll,
            win_probability=win_probability,
            payout_odds=payout_odds,
            edge=edge,
            current_aggregate_exposure=exposure,
            market_price=market_price,
        )

        bet_size = result.size if hasattr(result, "size") else _decimal_from_runtime(result)

        if self.charlie:
            charlie_confidence = _decimal_from_runtime(
                opportunity.get("charlie_confidence", self.charlie.last_confidence)
            )
            multiplier = self.charlie.calculate_kelly_multiplier(
                charlie_confidence,
                edge,
            )
            bet_size = bet_size * _decimal_from_runtime(multiplier)

        max_bet = bankroll * self.max_trade_pct
        if bet_size > max_bet:
            bet_size = max_bet

        return quantize_quantity(bet_size)

    @staticmethod
    def calculate_kelly(win_prob: Decimal, payout_odds: Decimal) -> Decimal:
        win_prob_dec = _decimal_from_runtime(win_prob)
        payout_odds_dec = _decimal_from_runtime(payout_odds)
        if payout_odds_dec <= Decimal("1"):
            return Decimal("0")
        b = payout_odds_dec - Decimal("1")
        q = Decimal("1") - win_prob_dec
        kelly = (b * win_prob_dec - q) / b
        return max(Decimal("0"), kelly)

    async def _current_aggregate_exposure(self) -> Decimal:
        if not self.ledger or not hasattr(self.ledger, "get_open_positions"):
            return Decimal("0")

        try:
            positions = await self.ledger.get_open_positions()
        except Exception:
            return Decimal("0")

        total = Decimal("0")
        for position in positions:
            try:
                entry_price = Decimal(str(position.entry_price))
                quantity = Decimal(str(position.quantity))
                total += entry_price * quantity
            except Exception:
                continue

        return total
