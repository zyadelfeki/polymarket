"""
Charlie Prediction Gate for Polymarket.

This module is the **only authorised entry point** for deciding whether to
place a bet.  A bet may only be placed when:

  1. A Charlie signal is available (p_win returned, not stale/degraded).
  2. The edge ``p_win - implied_prob`` meets ``MIN_EDGE``.
  3. Charlie's ``confidence`` meets ``MIN_CONFIDENCE``.
  4. The regime filter passes (configurable; defaults to allowing all regimes).

If any condition fails, ``evaluate_market`` returns ``None`` and ``main.py``
must not submit an order.

Usage::

    booster = CharliePredictionGate(kelly_sizer=sizer)
    rec = await booster.evaluate_market(
        market_id="0xabc...",
        market_price=Decimal("0.62"),
        symbol="BTC",
        timeframe="15m",
        bankroll=Decimal("50.00"),
    )
    if rec is None:
        return  # no signal → no bet
    # rec.side, rec.size, rec.kelly_fraction, rec.reason are available

Backward compatibility
----------------------
The old ``predict_15min_move`` / ``calculate_kelly_multiplier`` / ``should_trade``
API is replaced.  ``predict_15min_move`` is kept as a deprecated shim only.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional
import structlog

# structlog is used throughout the polymarket codebase — stdlib logging does
# NOT accept keyword arguments (reason=, market_id=, …) so all structured
# log calls here must go through structlog.
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Import Charlie signal API
# ---------------------------------------------------------------------------

_get_signal_for_market = None


def _load_charlie_api():
    global _get_signal_for_market
    if _get_signal_for_market is not None:
        return True

    # Option A: project-charlie installed as package / on PYTHONPATH
    try:
        from src.api.signals import get_signal_for_market  # type: ignore
        _get_signal_for_market = get_signal_for_market
        logger.info("charlie_api_loaded_from_package")
        return True
    except ImportError:
        pass

    # Option B: CHARLIE_PATH env var points to the project-charlie repo root
    charlie_root = os.getenv("CHARLIE_PATH", "")
    if charlie_root and charlie_root not in sys.path:
        sys.path.insert(0, charlie_root)
        try:
            from src.api.signals import get_signal_for_market  # type: ignore
            _get_signal_for_market = get_signal_for_market
            logger.info("charlie_api_loaded_via_CHARLIE_PATH", path=charlie_root)
            return True
        except ImportError:
            pass

    logger.warning(
        "charlie_api_unavailable — all bets will be blocked until Charlie is reachable. "
        "Set CHARLIE_PATH to the project-charlie repo root to resolve."
    )
    return False


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TradeRecommendation:
    """
    Affirmative trade recommendation from the Charlie gate.

    Only returned when ALL filters pass.  Never returned with size == 0.
    """
    side:             str       # "YES" or "NO"
    size:             Decimal   # USDC amount, Kelly-sized
    kelly_fraction:   Decimal   # fraction of bankroll
    p_win:            float
    implied_prob:     float
    edge:             float
    confidence:       float
    regime:           str       # directional: BULLISH | BEARISH | NEUTRAL
    technical_regime: str       # structural: TRENDING | MEAN_REVERTING | HIGH_VOL | LOW_VOL | UNKNOWN
    reason:           str       # structured string for order log
    # model_votes is optional: {"random_forest": "BUY", "svm": "HOLD", ...}
    # Stored in order_tracking.model_votes for per-model feedback on settlement.
    model_votes:      Optional[Dict] = None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class CharliePredictionGate:
    """
    Evaluates each candidate market against Charlie's signal and configured
    thresholds.  Returns a ``TradeRecommendation`` or ``None``.

    Parameters
    ----------
    kelly_sizer:
        Instance of ``risk.kelly_sizing.KellySizer``.
    min_edge:
        Minimum ``p_win - implied_prob`` to allow a bet (Decimal).
    min_confidence:
        Minimum Charlie confidence to allow a bet (Decimal).
    allowed_regimes:
        Set of regime strings that are acceptable.  None means all regimes OK.
    signal_timeout:
        Seconds to wait for Charlie's signal API before giving up.
    """

    def __init__(
        self,
        kelly_sizer=None,
        *,
        min_edge: Decimal = Decimal("0.05"),
        min_confidence: Decimal = Decimal("0.60"),
        allowed_regimes: Optional[set] = None,
        signal_timeout: float = 8.0,
    ) -> None:
        self._kelly_sizer = kelly_sizer
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._allowed_regimes = allowed_regimes  # None = allow all regimes
        self._signal_timeout = signal_timeout
        _load_charlie_api()

    # ------------------------------------------------------------------ public

    async def evaluate_market(
        self,
        *,
        market_id: str,
        market_price: Decimal,
        symbol: str = "BTC",
        timeframe: str = "15m",
        bankroll: Decimal = Decimal("0"),
        extra_features: Optional[Dict] = None,
        override_win_rate: Optional[float] = None,
    ) -> Optional[TradeRecommendation]:
        """
        Evaluate one candidate market.

        Returns ``None`` (no bet) if any filter fails.
        Returns a ``TradeRecommendation`` only when all filters pass.
        """
        if _get_signal_for_market is None:
            logger.warning(
                "charlie_gate_blocked",
                reason="charlie_api_unavailable",
                market_id=market_id,
            )
            return None

        # --- 1. Fetch Charlie signal ----------------------------------------
        try:
            signal = await asyncio.wait_for(
                _get_signal_for_market(symbol, timeframe, extra_features=extra_features),
                timeout=self._signal_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "charlie_gate_blocked",
                reason="signal_timeout",
                market_id=market_id,
                symbol=symbol,
            )
            return None
        except Exception as exc:
            logger.warning(
                "charlie_gate_blocked",
                reason="signal_error",
                market_id=market_id,
                error=str(exc),
            )
            return None

        p_win: float = float(signal["p_win"])
        confidence: float = float(signal["confidence"])
        regime: str = signal["regime"]
        technical_regime: str = signal.get("technical_regime", "UNKNOWN")
        # model_votes may be present if Charlie's ensemble exposes them in the signal dict
        model_votes: Optional[Dict] = signal.get("model_votes", None)

        # --- 2. Compute edge (evaluate both YES and NO directions) ----------
        yes_implied = float(market_price)
        yes_edge = p_win - yes_implied
        no_p_win = 1.0 - p_win
        no_implied = 1.0 - yes_implied
        no_edge = no_p_win - no_implied

        if yes_edge >= no_edge:
            side = "YES"
            effective_p_win = p_win
            implied_prob = yes_implied
            edge = yes_edge
        else:
            side = "NO"
            effective_p_win = no_p_win
            implied_prob = no_implied
            edge = no_edge

        # --- 2b. Coin-flip rejection: p_win within 3% of 0.5 = no signal ----
        # Charlie is operating in degraded/neutral mode when it cannot
        # distinguish direction.  A p_win of 0.5 ± 0.03 means the model has
        # zero conviction — trading on it is pure noise.  Hard-block these.
        if abs(p_win - 0.5) < 0.03:
            logger.warning(
                "charlie_coin_flip_rejected",
                market_id=market_id,
                p_win=p_win,
                reason="p_win within 3% of 0.5 = no signal",
                symbol=symbol,
            )
            return None

        # --- 3. Edge filter -------------------------------------------------
        if Decimal(str(edge)) < self._min_edge:
            logger.info(
                "charlie_gate_blocked",
                reason="edge_below_threshold",
                market_id=market_id,
                edge=f"{edge:.4f}",
                min_edge=str(self._min_edge),
                p_win=p_win,
                implied_prob=yes_implied,
                symbol=symbol,
            )
            return None

        # --- 4. Confidence filter ------------------------------------------
        if Decimal(str(confidence)) < self._min_confidence:
            logger.info(
                "charlie_gate_blocked",
                reason="confidence_below_threshold",
                market_id=market_id,
                confidence=f"{confidence:.4f}",
                min_confidence=str(self._min_confidence),
                symbol=symbol,
            )
            return None

        # --- 5. Regime filter ----------------------------------------------
        if self._allowed_regimes is not None and regime not in self._allowed_regimes:
            logger.info(
                "charlie_gate_blocked",
                reason="regime_filtered",
                market_id=market_id,
                regime=regime,
                allowed=list(self._allowed_regimes),
            )
            return None

        # --- 6. Kelly sizing (smooth multiplier applied to reduce position
        #       size proportionally as edge approaches the minimum threshold) --
        size = Decimal("0")
        kelly_fraction = Decimal("0")

        # Smooth ramp so size scales continuously with edge quality rather than
        # jumping from zero to full-Kelly at the threshold boundary.
        smooth_mult = self._edge_to_kelly_multiplier(
            edge=edge,
            min_edge=float(self._min_edge),
            ramp_width=0.05,
        )
        # If the ramp gives 0 there is no edge (already caught above, but be
        # defensive).
        if smooth_mult <= 0.0:
            logger.info(
                "charlie_gate_blocked",
                reason="smooth_kelly_multiplier_zero",
                market_id=market_id,
                edge=f"{edge:.4f}",
            )
            return None

        if self._kelly_sizer is not None and bankroll > Decimal("0"):
            kelly_result = self._kelly_sizer.compute_size(
                p_win=effective_p_win,
                implied_prob=implied_prob,
                bankroll=bankroll,
                override_win_rate=override_win_rate,
            )
            # Scale raw Kelly size by smooth multiplier before applying caps.
            raw_size = kelly_result.size * Decimal(str(round(smooth_mult, 6)))
            size = raw_size
            kelly_fraction = kelly_result.effective_fraction * Decimal(str(round(smooth_mult, 6)))

            if size <= Decimal("0"):
                logger.info(
                    "charlie_gate_blocked",
                    reason="kelly_size_zero",
                    market_id=market_id,
                    capped_reason=kelly_result.capped_reason,
                )
                return None

        reason = (
            f"charlie_signal side={side} p_win={effective_p_win:.3f} "
            f"implied={implied_prob:.3f} edge={edge:.3f} "
            f"conf={confidence:.3f} regime={regime}"
        )

        recommendation = TradeRecommendation(
            side=side,
            size=size,
            kelly_fraction=kelly_fraction,
            p_win=effective_p_win,
            implied_prob=implied_prob,
            edge=edge,
            confidence=confidence,
            regime=regime,
            technical_regime=technical_regime,
            reason=reason,
            model_votes=model_votes,
        )

        logger.info(
            "charlie_gate_approved",
            market_id=market_id,
            side=side,
            size=str(size),
            kelly_fraction=str(kelly_fraction),
            p_win=effective_p_win,
            implied_prob=implied_prob,
            edge=edge,
            confidence=confidence,
            regime=regime,
            symbol=symbol,
        )

        return recommendation

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def compute_edge_vs_market(
        charlie_prob: Decimal,
        market_price: Decimal,
        fee_bps: int = 100,
    ) -> Decimal:
        """
        Net edge after fees: (charlie_probability - market_price) - fee.

        fee_bps: taker fee expressed in basis points (100 bps = 1.0%).
        Returns a signed Decimal; negative means no edge after costs.

        Why expose this as a standalone method: callers (e.g. scan loops in
        LatencyArbitrageEngine) need a cheap pre-filter that never touches the
        Charlie API.  Run this first; only call ``evaluate_market`` when the
        result is positive.
        """
        fee_rate = Decimal(str(fee_bps)) / Decimal("10000")
        return Decimal(str(charlie_prob)) - Decimal(str(market_price)) - fee_rate

    @staticmethod
    def _edge_to_kelly_multiplier(
        edge: float,
        min_edge: float,
        ramp_width: float = 0.05,
    ) -> float:
        """
        Smooth ramp from 0 → 1 as edge rises above min_edge.

        Replaces a hard step-function (0 / 1) with a linear ramp over
        ``ramp_width``, so position size grows continuously with edge quality
        instead of jumping from 0 to full Kelly the moment the threshold is
        crossed.

        ramp_width=0.05 means full Kelly is reached at min_edge + 5 pp.
        Below min_edge the multiplier is 0 (no bet); above min_edge+ramp it
        is 1.0 (full Kelly fraction).
        """
        if edge <= min_edge:
            return 0.0
        if ramp_width <= 0:
            return 1.0
        return min(1.0, (edge - min_edge) / ramp_width)

    # ------------------------------------------------------------------ compat

    async def predict_15min_move(
        self,
        symbol: str = "BTC",
        *,
        extra_features: Optional[Dict] = None,
        **_kwargs,
    ) -> Dict:
        """
        Deprecated shim — kept for any existing call-sites.
        Migrate to ``evaluate_market`` for all new code.

        ``extra_features`` is forwarded to ``get_signal_for_market`` so that
        real Binance indicators are used instead of synthetic fallback values.
        """
        if _get_signal_for_market is None:
            return {"probability": 0.5, "confidence": 0.0, "direction": "NEUTRAL"}
        try:
            sig = await asyncio.wait_for(
                _get_signal_for_market(symbol, "15m", extra_features=extra_features),
                timeout=self._signal_timeout,
            )
            p_win = sig["p_win"]
            direction = "UP" if p_win > 0.5 else ("DOWN" if p_win < 0.5 else "NEUTRAL")
            return {"probability": p_win, "confidence": sig["confidence"], "direction": direction}
        except Exception:
            return {"probability": 0.5, "confidence": 0.0, "direction": "NEUTRAL"}
