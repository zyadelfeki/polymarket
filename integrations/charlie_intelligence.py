from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union

logger = logging.getLogger(__name__)


class CharlieIntelligence:
    """
    Consumes intelligence signals from project-charlie.
    """

    def __init__(self, shared_file: Optional[Union[str, Path]] = None):
        if shared_file is None:
            shared_file = self._default_shared_file()
        self.shared_file = Path(shared_file)
        self.last_signal: Optional[Dict] = None

    async def get_signal(self) -> Optional[Dict]:
        """
        Get latest intelligence signal.

        Returns None if data unavailable or stale (>60s old).
        """
        try:
            if not self.shared_file.exists():
                return None

            data = json.loads(self.shared_file.read_text())
            ts_raw = data.get("timestamp")
            if not ts_raw:
                return None

            signal_time = self._parse_timestamp(ts_raw)
            age_seconds = (datetime.now(timezone.utc) - signal_time).total_seconds()

            if age_seconds > 60:
                logger.warning(f"Charlie intelligence stale ({age_seconds:.1f}s)")
                return None

            self.last_signal = data
            return data

        except Exception as exc:
            logger.error(f"Failed to read intelligence: {exc}")
            return None

    def boost_confidence(self, base_confidence: float, position_direction: str) -> float:
        """
        Boost confidence using Charlie's intelligence.

        Adds:
        - 10% if LSTM agrees with direction
        - 5% if whale signal agrees
        - -5% if MEV volatility > 0.7
        """
        if not self.last_signal:
            return base_confidence

        boost = 0.0

        lstm_direction = self.last_signal.get("lstm_direction")
        if position_direction == "UP" and lstm_direction == "UP":
            boost += 0.10
        elif position_direction == "DOWN" and lstm_direction == "DOWN":
            boost += 0.10

        whale_signal = self.last_signal.get("whale_signal")
        if position_direction == "UP" and whale_signal == "BULLISH":
            boost += 0.05
        elif position_direction == "DOWN" and whale_signal == "BEARISH":
            boost += 0.05

        try:
            mev_volatility = float(self.last_signal.get("mev_volatility_score", 0.0))
            if mev_volatility > 0.7:
                boost -= 0.05
        except (TypeError, ValueError):
            pass

        boosted = base_confidence + boost
        boosted = min(boosted, 0.95)
        return max(0.0, boosted)

    def _parse_timestamp(self, value: str) -> datetime:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _default_shared_file(self) -> str:
        env_path = os.getenv("CHARLIE_INTELLIGENCE_FILE")
        if env_path:
            return env_path

        if os.name == "nt":
            return str(Path(tempfile.gettempdir()) / "charlie_intelligence.json")

        return "/tmp/charlie_intelligence.json"
