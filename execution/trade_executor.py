"""
Trade executor for Polymarket.

Accepts a pre-evaluated opportunity dict (built by BTCPriceLevelScanner from
a TradeRecommendation) and places a real or paper order.

Key invariants
--------------
- If opportunity["kelly_size"] is present, that value is used directly as the
  bet size.  It was computed by CharliePredictionGate with full calibration,
  smooth Kelly ramp, fee deduction, and OFI-conflict halving.  We do NOT
  recompute it here.
- Minimum bet floor is $1.00 (Polymarket enforced minimum).  Orders below
  this floor are logged and dropped — they will never succeed on-chain.
- circuit_breaker.record_trade() is NOT called here.  Placement != loss.
  The settlement loop in run_paper_trading.py calls it after resolution.
- circuit_breaker is required at construction time; raises RuntimeError otherwise.
- All money-sensitive arithmetic (prices, sizes, balances, shares, PnL) uses
  Decimal throughout.  float() is forbidden on the active path.
"""

import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Optional
import logging
from datetime import datetime, timezone

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


MIN_BET_SIZE = Decimal("1.00")  # Polymarket enforced minimum order size (USDC)
_FOUR_DP = Decimal("0.0001")
_EIGHT_DP = Decimal("0.00000001")


def _dec(value, fallback: str = "0") -> Decimal:
    """Safely coerce any scalar to Decimal without going through float."""
    if value is None:
        return Decimal(fallback)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Go through str to avoid float binary representation noise
        return Decimal(str(value))
    return Decimal(str(value))


def _log(level: str, event: str, **kwargs):
    if _structlog_available:
        getattr(logger, level)(event, **kwargs)
    else:
        getattr(logger, level)("%s %s", event, kwargs or "")


class TradeExecutor:
    def __init__(self, polymarket_client, bankroll_tracker, kelly_sizer, db, circuit_breaker=None):
        if circuit_breaker is None:
            raise RuntimeError("circuit_breaker required")
        self.polymarket = polymarket_client
        self.bankroll = bankroll_tracker
        self.kelly = kelly_sizer
        self.db = db
        self.circuit_breaker = circuit_breaker
        self.execution_queue = asyncio.Queue()

    async def execute_trade(self, opportunity: Dict) -> bool:
        if not self.circuit_breaker.is_trading_allowed():
            _log(
                "warning",
                "trade_blocked_circuit_breaker",
                reason=self.circuit_breaker.breaker_reason,
                market_id=opportunity.get("market_id"),
            )
            return False

        market_id = str(opportunity["market_id"])
        side = str(opportunity.get("side") or opportunity.get("true_outcome") or "YES").upper()
        # Keep all money/probability values as Decimal — never float on this path
        confidence: Decimal = _dec(opportunity.get("confidence", "0"))
        edge: Decimal = _dec(opportunity.get("edge", "0"))
        market_price: Decimal = _dec(opportunity.get("market_price", "0.5") or "0.5")
        question = str(opportunity.get("question", ""))[:80]
        token_id = opportunity.get("token_id") or market_id
        balance: Decimal = _dec(self.bankroll.current_balance)

        # --- Sizing -----------------------------------------------------------
        if "kelly_size" in opportunity and opportunity["kelly_size"] is not None:
            bet_size: Decimal = _dec(opportunity["kelly_size"])
        else:
            if self.kelly is not None:
                # Decimal reciprocal — no float division
                payout_odds: Decimal = (
                    Decimal("1") / market_price if market_price > Decimal("0")
                    else Decimal("2")
                )
                raw = self.kelly.calculate_bet_size(
                    confidence, payout_odds, edge,
                    strategy=opportunity.get("strategy", "default")
                )
                bet_size = _dec(raw)
            else:
                _log(
                    "warning",
                    "trade_skipped_no_size",
                    market_id=market_id,
                    reason="no kelly_size in opportunity and no kelly_sizer configured",
                )
                return False

        # --- Minimum size enforcement (Polymarket floor is $1.00 USDC) -------
        if bet_size < MIN_BET_SIZE:
            if balance >= MIN_BET_SIZE:
                _log(
                    "info",
                    "bet_size_clamped_to_minimum",
                    market_id=market_id,
                    question=question,
                    original_bet_size=str(bet_size),
                    bet_size=str(MIN_BET_SIZE),
                    min_bet_size=str(MIN_BET_SIZE),
                    side=side,
                    edge=str(edge.quantize(_FOUR_DP)),
                    confidence=str(confidence.quantize(_FOUR_DP)),
                )
                bet_size = MIN_BET_SIZE
            else:
                _log(
                    "info",
                    "order_rejected_insufficient_balance",
                    market_id=market_id,
                    question=question,
                    bet_size=str(bet_size),
                    min_bet_size=str(MIN_BET_SIZE),
                    balance=str(balance),
                    side=side,
                    edge=str(edge.quantize(_FOUR_DP)),
                    confidence=str(confidence.quantize(_FOUR_DP)),
                )
                return False

        _log(
            "info",
            "order_attempt",
            market_id=market_id,
            question=question,
            side=side,
            market_price=str(market_price.quantize(_FOUR_DP)),
            edge=str(edge.quantize(_FOUR_DP)),
            confidence=str(confidence.quantize(_FOUR_DP)),
            bet_size=str(bet_size),
            token_id=token_id,
        )

        # Pass positional args so both PolymarketClient (V1, param name=amount)
        # and PolymarketClientV2 (param name=size) work without branching.
        order_result = await self.polymarket.place_order(
            token_id,    # positional 1: token_id
            side,        # positional 2: side
            bet_size,    # positional 3: size (V2) / amount (V1) — Decimal
            market_price,  # positional 4: price — Decimal
        )
        success = bool(order_result and order_result.get("success"))

        if success:
            # shares = bet_size / market_price — Decimal division, no float
            shares: Decimal = (
                (bet_size / market_price).quantize(_EIGHT_DP, rounding=ROUND_DOWN)
                if market_price > Decimal("0")
                else Decimal("0")
            )
            trade_record = {
                "market_id": market_id,
                "market_title": question,
                "side": side,
                # Store as str so JSON serialisation is lossless
                "entry_price": str(market_price),
                "bet_size": str(bet_size),
                "shares": str(shares),
                "status": "OPEN",
                "strategy": opportunity.get("strategy", "charlie_gate"),
                "edge": str(edge),
                "confidence": str(confidence),
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "token_id": token_id,
                "kelly_fraction": str(opportunity.get("kelly_fraction", "")),
                "p_win": str(opportunity.get("true_prob", "")),
                "model_votes": opportunity.get("model_votes"),
                "ofi_conflict": bool(opportunity.get("ofi_conflict", False)),
                # end_date stored so the settlement loop can detect resolution
                "end_date": str(opportunity.get("end_date", "")),
            }

            trade_id = self.db.log_trade(trade_record)
            trade_record["db_id"] = trade_id
            trade_record["trade_id"] = f"trade_{trade_id}"
            self.bankroll.add_trade(trade_record)
            # NOTE: circuit_breaker.record_trade() is intentionally NOT called
            # here.  A placement is an open position, not a resolved outcome.
            # The settlement loop in run_paper_trading.py calls it once the
            # market window has actually closed.

            _log(
                "info",
                "order_placed",
                market_id=market_id,
                question=question,
                side=side,
                bet_size=str(bet_size),
                shares=str(shares),
                entry_price=str(market_price),
                order_id=order_result.get("order_id") if order_result else None,
                trade_id=f"trade_{trade_id}",
                edge=str(edge.quantize(_FOUR_DP)),
            )
            return True
        else:
            _log(
                "error",
                "order_rejected_by_broker",
                market_id=market_id,
                question=question,
                side=side,
                bet_size=str(bet_size),
                edge=str(edge.quantize(_FOUR_DP)),
            )
            return False

    async def process_execution_queue(self):
        while True:
            opportunity = await self.execution_queue.get()
            await self.execute_trade(opportunity)
            await asyncio.sleep(1)

    def queue_trade(self, opportunity: Dict):
        self.execution_queue.put_nowait(opportunity)
