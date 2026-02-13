from decimal import Decimal
from typing import Optional, Dict

try:
    import structlog

    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

if _structlog_available:
    logger = structlog.get_logger()
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)


def _log(level: str, event: str, **kwargs) -> None:
    if _structlog_available:
        getattr(logger, level)(event, **kwargs)
        return

    if kwargs:
        details = " ".join(f"{key}={value}" for key, value in kwargs.items())
        message = f"{event} | {details}"
    else:
        message = event

    getattr(logger, level)(message)


class ConfidenceBooster:
    """Apply Charlie intelligence to boost/penalize trade confidence"""

    def __init__(self, redis_subscriber):
        self.redis_sub = redis_subscriber
        _log("info", "confidence_booster_initialized")

    def apply_boost(self, base_confidence: Decimal, trade_direction: str) -> Decimal:
        """
        Adjust confidence based on Charlie signals

        Args:
            base_confidence: Initial confidence from price analysis (0.0-1.0)
            trade_direction: "UP" or "DOWN"

        Returns:
            Adjusted confidence clamped to [0, 1]
        """
        intel = self.redis_sub.get_intelligence()
        if not intel:
            _log("debug", "no_intelligence_available", using_base=float(base_confidence))
            return base_confidence

        boosted = base_confidence
        boosts_applied = []

        # LSTM alignment bonus (+10% of LSTM confidence)
        if intel['lstm_prediction'] == trade_direction:
            boost = Decimal(str(intel['lstm_confidence'])) * Decimal("0.10")
            boosted += boost
            boosts_applied.append(f"LSTM+{boost:.3f}")
            _log(
                "info",
                "lstm_boost_applied",
                prediction=intel['lstm_prediction'],
                confidence=intel['lstm_confidence'],
                boost=float(boost),
            )

        # Whale flow confirmation (+5%)
        whale_flow = Decimal(str(intel['whale_flow']))
        if (trade_direction == "UP" and whale_flow > 0) or \
           (trade_direction == "DOWN" and whale_flow < 0):
            boosted += Decimal("0.05")
            boosts_applied.append("WHALE+0.05")
            _log(
                "info",
                "whale_confirmation",
                direction=trade_direction,
                flow=float(whale_flow),
            )

        # MEV volatility penalty (-5%)
        mev_vol = Decimal(str(intel['mev_volatility']))
        if mev_vol > Decimal("0.7"):
            boosted -= Decimal("0.05")
            boosts_applied.append("MEV-0.05")
            _log(
                "warning",
                "high_volatility_penalty",
                mev_volatility=float(mev_vol),
            )

        # Clamp to valid probability range [0, 1]
        final_confidence = max(Decimal("0"), min(boosted, Decimal("1")))

        adjustment = final_confidence - base_confidence
        _log(
            "info",
            "confidence_adjusted",
            base=float(base_confidence),
            final=float(final_confidence),
            adjustment=float(adjustment),
            boosts=" ".join(boosts_applied) if boosts_applied else "none",
        )

        return final_confidence
