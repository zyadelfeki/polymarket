"""
Use Charlie LSTM to BOOST position sizing when predictions align.
"""

from __future__ import annotations

from typing import Optional, Dict, Union

from decimal import Decimal

from integrations.charlie_intelligence import CharlieIntelligence
from utils.decimal_helpers import safe_decimal


class CharliePredictionBooster:
    def __init__(self, intelligence: Optional[CharlieIntelligence] = None) -> None:
        self.intelligence = intelligence or CharlieIntelligence()
        self.last_signal: Optional[Dict] = None
        self.last_confidence: float = 0.0

    async def get_signal(self) -> Optional[Dict]:
        signal = await self.intelligence.get_signal()
        if signal:
            self.last_signal = signal
        return self.last_signal

    async def predict_15min_move(self, **kwargs) -> Dict:
        """
        Normalize Charlie LSTM output into a {probability, confidence, direction} payload.
        """
        signal = await self.get_signal()
        if not signal:
            return {"probability": 0.5, "confidence": 0.5, "direction": "NEUTRAL"}

        direction_raw = (
            signal.get("direction")
            or signal.get("lstm_direction")
            or signal.get("trend")
            or "NEUTRAL"
        )
        direction = self._normalize_direction(direction_raw)

        confidence = self._safe_float(
            signal.get("confidence")
            or signal.get("lstm_confidence")
            or signal.get("probability_confidence")
            or 0.0
        )

        probability = signal.get("probability") or signal.get("lstm_probability")
        if probability is None:
            prob_up = signal.get("lstm_prob_up")
            prob_down = signal.get("lstm_prob_down")
            if prob_up is not None:
                probability = prob_up
            elif prob_down is not None:
                probability = 1.0 - self._safe_float(prob_down)

        probability = self._safe_float(probability)
        if probability == 0.0:
            if direction == "UP":
                probability = 0.5 + (confidence / 2)
            elif direction == "DOWN":
                probability = 0.5 - (confidence / 2)
            else:
                probability = 0.5

        probability = min(max(probability, 0.01), 0.99)
        confidence = min(max(confidence, 0.0), 0.99)

        self.last_confidence = confidence
        return {
            "probability": probability,
            "confidence": confidence,
            "direction": direction,
        }

    def calculate_kelly_multiplier(
        self,
        charlie_confidence: Union[Decimal, float, int, str],
        latency_edge: Union[Decimal, float, int, str],
    ) -> Decimal:
        """
        When Charlie agrees with latency signal → increase bet size.
        Returns a Decimal multiplier.
        """
        confidence_dec = safe_decimal(charlie_confidence)
        edge_dec = safe_decimal(latency_edge)

        base_kelly = Decimal("0.10")

        if confidence_dec > Decimal("0.70") and edge_dec > Decimal("0.05"):
            multiplier = Decimal("2.0")
        elif confidence_dec > Decimal("0.60") and edge_dec > Decimal("0.03"):
            multiplier = Decimal("1.5")
        elif edge_dec > Decimal("0.10"):
            multiplier = Decimal("1.8")
        else:
            multiplier = Decimal("1.0")

        return base_kelly * multiplier

    async def should_trade(self, latency_signal: Dict) -> bool:
        """
        Veto system: if Charlie says SELL but latency says BUY, skip.
        """
        signal = await self.get_signal()
        if not signal:
            return True

        latency_direction = self._extract_latency_direction(latency_signal)
        charlie_direction = self._normalize_direction(
            signal.get("direction") or signal.get("lstm_direction") or "NEUTRAL"
        )

        if charlie_direction == "NEUTRAL" or latency_direction == "NEUTRAL":
            return True

        if charlie_direction != latency_direction:
            return False

        return True

    def _extract_latency_direction(self, latency_signal: Dict) -> str:
        if not latency_signal:
            return "NEUTRAL"
        direction = latency_signal.get("direction")
        if direction:
            return self._normalize_direction(direction)
        side = latency_signal.get("side")
        if str(side).upper() == "YES":
            return "UP"
        if str(side).upper() == "NO":
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _normalize_direction(value: str) -> str:
        raw = str(value).upper()
        if raw in {"UP", "LONG", "BUY", "BULL", "BULLISH", "YES"}:
            return "UP"
        if raw in {"DOWN", "SHORT", "SELL", "BEAR", "BEARISH", "NO"}:
            return "DOWN"
        return "NEUTRAL"

    @staticmethod
    def _safe_float(value) -> float:
        try:
            if value is None:
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0
