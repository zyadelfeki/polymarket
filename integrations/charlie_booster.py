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

from models.calibration import calibrate_p_win
from utils.fee_calculator import taker_fee_rate, net_edge as _net_edge_calc
from data_feeds.ofi_calculator import OFICalculator

# structlog is used throughout the polymarket codebase — stdlib logging does
# NOT accept keyword arguments (reason=, market_id=, …) so all structured
# log calls here must go through structlog.
logger = structlog.get_logger(__name__)

# Module-level OFI calculator.  One instance per process; its rolling window
# is seeded from Binance orderbook snapshots injected via extra_features.
# Resets on restart — intentional (stale history from a dead session is
# worse than no history).
_ofi_calc = OFICalculator(window_seconds=180)


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
    p_win_raw:        float
    p_win_calibrated: float
    implied_prob:     float
    edge:             float
    confidence:       float
    regime:           str       # directional: BULLISH | BEARISH | NEUTRAL
    technical_regime: str       # structural: TRENDING | MEAN_REVERTING | HIGH_VOL | LOW_VOL | UNKNOWN
    reason:           str       # structured string for order log
    # model_votes is optional: {"random_forest": "BUY", "svm": "HOLD", ...}
    # Stored in order_tracking.model_votes for per-model feedback on settlement.
    model_votes:      Optional[Dict] = None
    # True when OFI direction conflicted with Charlie and size was halved.
    # Set by the OFI filter inside _evaluate_market_inner.
    ofi_conflict:     bool = False


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
        coin_flip_reject_band_abs: Optional[Decimal] = None,
    ) -> None:
        self._kelly_sizer = kelly_sizer
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._allowed_regimes = allowed_regimes  # None = allow all regimes
        self._signal_timeout = signal_timeout
        if coin_flip_reject_band_abs is None:
            coin_flip_reject_band_abs = Decimal(
                os.getenv("CHARLIE_COIN_FLIP_REJECT_BAND_ABS", "0.03")
            )
        self._coin_flip_reject_band_abs = Decimal(str(coin_flip_reject_band_abs))
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
        market_question: str = "",
        override_win_rate: Optional[float] = None,
    ) -> Optional[TradeRecommendation]:
        """
        Evaluate one candidate market.

        Returns ``None`` (no bet) if any filter fails.
        Returns a ``TradeRecommendation`` only when all filters pass.

        ``market_question`` is used for BTC-relevance guard and log enrichment.
        """
        try:
            return await self._evaluate_market_inner(
                market_id=market_id,
                market_price=market_price,
                symbol=symbol,
                timeframe=timeframe,
                bankroll=bankroll,
                extra_features=extra_features,
                market_question=market_question,
                override_win_rate=override_win_rate,
            )
        except Exception as exc:
            logger.error(
                "charlie_gate_exception",
                market_id=market_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    async def _evaluate_market_inner(
        self,
        *,
        market_id: str,
        market_price: Decimal,
        symbol: str = "BTC",
        timeframe: str = "15m",
        bankroll: Decimal = Decimal("0"),
        extra_features: Optional[Dict] = None,
        market_question: str = "",
        override_win_rate: Optional[float] = None,
    ) -> Optional[TradeRecommendation]:
        """Inner implementation of evaluate_market, wrapped by the outer try/except."""

        # --- 0. BTC-relevance guard ----------------------------------------
        # Charlie's ensemble models are trained exclusively on BTC OHLCV
        # indicators (RSI-14, MACD, price-vs-SMA, book imbalance).  Applying
        # those features to Solana / ETH / XRP markets produces p_win values
        # that have zero predictive power for the non-BTC asset.
        # Skip markets whose symbol AND question text contain no BTC keyword.
        _sym_upper = symbol.strip().upper()
        _is_btc_symbol = _sym_upper.startswith("BTC") or _sym_upper == "XBTUSDT"
        _q_lower = market_question.lower()
        _has_btc_keyword = "bitcoin" in _q_lower or "btc" in _q_lower
        if not _is_btc_symbol and not _has_btc_keyword:
            logger.info(
                "charlie_skipped_irrelevant_market",
                market_id=market_id,
                symbol=symbol,
                market_question=market_question[:80],
                reason="charlie_models_are_btc_only_no_btc_keyword_in_question",
            )
            return None

        if _get_signal_for_market is None:
            logger.warning(
                "charlie_gate_rejected",
                reason="charlie_api_unavailable",
                market_id=market_id,
                market_question=market_question[:80],
                p_win=None,
                min_win_probability=None,
                confidence=None,
                ensemble_votes=None,
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
                "charlie_gate_rejected",
                reason="signal_timeout",
                market_id=market_id,
                symbol=symbol,
                p_win=None,
                min_win_probability=None,
            )
            return None
        except Exception as exc:
            logger.warning(
                "charlie_gate_rejected",
                reason="signal_error",
                market_id=market_id,
                error=str(exc),
                p_win=None,
            )
            return None

        p_win_raw: float = float(signal["p_win"])
        p_win: float = calibrate_p_win(p_win_raw)  # Platt-calibrated (passthrough if no scaler)
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
            gross_edge = yes_edge
        else:
            side = "NO"
            effective_p_win = no_p_win
            implied_prob = no_implied
            gross_edge = no_edge

        # --- 2c. Fee-aware edge: subtract dynamic Polymarket taker fee ------
        # Pass market_question so crypto direction markets pay the correct 3.15% rate
        _fee = float(taker_fee_rate(Decimal(str(implied_prob)), question=market_question))
        edge = gross_edge - _fee  # net edge after fees

        # --- 2b. Coin-flip rejection: p_win near 0.5 = no signal -------------
        # Charlie is operating in degraded/neutral mode when it cannot
        # distinguish direction.  By default, a p_win of 0.5 ± 0.03 means the
        # model has zero conviction — hard-block these.  A narrower band can be
        # enabled explicitly for controlled proof / operational diagnostics via
        # the constructor or `CHARLIE_COIN_FLIP_REJECT_BAND_ABS`.
        coin_flip_reject_band_abs = float(self._coin_flip_reject_band_abs)
        if abs(p_win - 0.5) < coin_flip_reject_band_abs:
            logger.warning(
                "charlie_coin_flip_rejected",
                market_id=market_id,
                market_question=market_question[:80],
                p_win=p_win,
                reason=f"p_win within {coin_flip_reject_band_abs:.4f} of 0.5 = no signal",
                symbol=symbol,
                coin_flip_reject_band_abs=coin_flip_reject_band_abs,
            )
            return None

        # --- 3. Edge filter (fee-aware) --------------------------------------
        if Decimal(str(edge)) < self._min_edge:
            logger.info(
                "charlie_gate_rejected",
                reason="edge_below_threshold",
                market_id=market_id,
                market_question=market_question[:80],
                gross_edge=f"{gross_edge:.4f}",
                net_edge=f"{edge:.4f}",
                fee=f"{_fee:.4f}",
                min_edge=str(self._min_edge),
                p_win=float(p_win),
                p_win_raw=float(p_win_raw),
                min_win_probability=None,
                implied_prob=yes_implied,
                confidence=float(confidence),
                ensemble_votes=str(model_votes),
                symbol=symbol,
            )
            return None

        # --- 4. Confidence filter ------------------------------------------
        if Decimal(str(confidence)) < self._min_confidence:
            logger.info(
                "charlie_gate_rejected",
                reason="confidence_below_threshold",
                market_id=market_id,
                market_question=market_question[:80],
                p_win=float(p_win),
                min_win_probability=None,
                confidence=float(confidence),
                min_confidence=str(self._min_confidence),
                ensemble_votes=str(model_votes),
                symbol=symbol,
            )
            return None

        # --- 5. Regime filter ----------------------------------------------
        if self._allowed_regimes is not None and regime not in self._allowed_regimes:
            logger.info(
                "charlie_gate_rejected",
                reason="regime_filtered",
                market_id=market_id,
                p_win=float(p_win),
                min_win_probability=None,
                confidence=float(confidence),
                regime=regime,
                allowed=list(self._allowed_regimes),
                ensemble_votes=str(model_votes),
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
                "charlie_gate_rejected",
                reason="smooth_kelly_multiplier_zero",
                market_id=market_id,
                p_win=float(p_win),
                min_win_probability=None,
                confidence=float(confidence),
                edge=f"{edge:.4f}",
                ensemble_votes=str(model_votes),
            )
            return None

        if self._kelly_sizer is not None and bankroll > Decimal("0"):
            # Guard: override_win_rate is the rolling global win rate across ALL markets.
            # If it is below implied_prob for THIS market, applying it as p_win in the
            # Kelly formula produces a negative fraction → size=0, even though Charlie
            # has genuine market-specific edge.  Drop the override in that case so the
            # sizing is based on Charlie's p_win, which IS market-specific.
            # Example failure mode: rolling_win_rate_20=0.45, implied_prob=0.50 →
            # (0.45-0.50)/(1-0.50) = -0.10 → size=0 with capped_reason=None (silent).
            effective_override = override_win_rate
            if override_win_rate is not None and override_win_rate < implied_prob:
                effective_override = None  # fall back to Charlie's p_win

            kelly_result = self._kelly_sizer.compute_size(
                p_win=effective_p_win,
                implied_prob=implied_prob,
                bankroll=bankroll,
                override_win_rate=effective_override,
            )
            # Scale raw Kelly size by smooth multiplier before applying caps.
            raw_size = kelly_result.size * Decimal(str(round(smooth_mult, 6)))
            size = raw_size
            kelly_fraction = kelly_result.effective_fraction * Decimal(str(round(smooth_mult, 6)))

            if size <= Decimal("0"):
                logger.info(
                    "charlie_gate_rejected",
                    reason="kelly_size_zero",
                    market_id=market_id,
                    market_question=market_question[:80],
                    p_win=float(effective_p_win),
                    min_win_probability=None,
                    confidence=float(confidence),
                    ensemble_votes=str(model_votes),
                    capped_reason=kelly_result.capped_reason,
                )
                return None

        reason = (
            f"charlie_signal side={side} p_win={effective_p_win:.3f} "
            f"implied={implied_prob:.3f} edge={edge:.3f} "
            f"fee={_fee:.4f} "
            f"conf={confidence:.3f} regime={regime} tech_regime={technical_regime}"
        )

        # --- 7. OFI confirmation / conflict filter --------------------------
        # Feed the latest Binance orderbook snapshot (injected by get_all_features
        # in binance_features.py) into the rolling OFI calculator, then check
        # whether the book pressure direction confirms or conflicts Charlie's
        # directional call.  Conflict → halve size (not block — Charlie signal
        # retains primacy; OFI is secondary confirmation).
        _ofi_signal: Optional[str] = None
        if extra_features:
            _ob_bids = extra_features.get("ofi_bids")
            _ob_asks = extra_features.get("ofi_asks")
            if _ob_bids and _ob_asks:
                _ofi_calc.add_snapshot(symbol, _ob_bids, _ob_asks)
            _ofi_signal = _ofi_calc.ofi_signal(symbol)

        _ofi_conflict_flag: bool = False
        if _ofi_signal is not None:
            # BUY signal from Charlie but book pressure is SELL → conflict
            # SELL signal from Charlie but book pressure is BUY → conflict
            _charlie_direction = "BUY" if side == "YES" else "SELL"
            if _ofi_signal != _charlie_direction:
                size = size * Decimal("0.5")
                kelly_fraction = kelly_fraction * Decimal("0.5")
                _ofi_conflict_flag = True
                logger.warning(
                    "ofi_conflict",
                    market_id=market_id,
                    symbol=symbol,
                    charlie_side=side,
                    ofi_signal=_ofi_signal,
                    size_after_halving=str(size),
                )
            else:
                logger.info(
                    "ofi_signal_confirmed",
                    market_id=market_id,
                    symbol=symbol,
                    side=side,
                    ofi_signal=_ofi_signal,
                )

        recommendation = TradeRecommendation(
            side=side,
            size=size,
            kelly_fraction=kelly_fraction,
            p_win=effective_p_win,
            p_win_raw=p_win_raw,
            p_win_calibrated=p_win,
            implied_prob=implied_prob,
            edge=edge,
            confidence=confidence,
            regime=regime,
            technical_regime=technical_regime,
            reason=reason,
            model_votes=model_votes,
            ofi_conflict=_ofi_conflict_flag,
        )

        logger.info(
            "charlie_gate_approved",
            market_id=market_id,
            market_question=market_question[:80],
            side=side,
            size=str(size),
            kelly_fraction=str(kelly_fraction),
            p_win=effective_p_win,
            p_win_raw=float(p_win_raw),
            p_win_calibrated=float(p_win),
            implied_prob=implied_prob,
            gross_edge=gross_edge,
            net_edge=edge,
            fee=_fee,
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


class CharliePredictionBooster:
    def __init__(self, intelligence, min_confidence: float = 0.60):
        self.intelligence = intelligence
        self.min_confidence = float(min_confidence)

    async def should_trade(self, latency_signal: dict) -> bool:
        signal = await self.intelligence.get_signal()
        direction = str(signal.get("lstm_direction", "")).upper()
        confidence = float(signal.get("lstm_confidence", 0.0))
        side = str(latency_signal.get("side", "")).upper()

        if confidence < self.min_confidence:
            return False

        if side == "YES":
            return direction == "UP"
        if side == "NO":
            return direction == "DOWN"

        return False


__all__ = ["TradeRecommendation", "CharliePredictionGate", "CharliePredictionBooster"]
