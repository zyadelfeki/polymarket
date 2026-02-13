from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

import aiohttp

logger = logging.getLogger(__name__)


class PriceFeedUnavailable(Exception):
    """Raised when no price source is available."""


class CharliePriceFeed:
    """
    Reads Bitcoin price published by project-charlie.
    Falls back to direct Binance API if unavailable or stale.
    """

    def __init__(self, shared_file: Optional[Union[str, Path]] = None):
        if shared_file is None:
            shared_file = self._default_shared_file()
        self.shared_file = Path(shared_file)

    async def get_btc_price(self) -> Decimal:
        """
        Get current BTC price from charlie or fallback to Binance.
        """
        try:
            price = self._read_shared_price()
            if price is not None:
                return price

            return await self._fetch_from_binance()
        except Exception as exc:
            logger.error(f"Price feed error: {exc}")
            return await self._fetch_from_binance()

    def _read_shared_price(self) -> Optional[Decimal]:
        if not self.shared_file.exists():
            return None

        try:
            data = json.loads(self.shared_file.read_text())
            ts_raw = data.get("timestamp")
            if not ts_raw:
                return None

            price_time = self._parse_timestamp(ts_raw)
            age_seconds = (datetime.now(timezone.utc) - price_time).total_seconds()

            if age_seconds < 10:
                return Decimal(str(data["price"]))

            logger.warning(f"Charlie price stale ({age_seconds:.1f}s old)")
            return None
        except Exception as exc:
            logger.warning(f"Failed to parse shared price: {exc}")
            return None

    async def _fetch_from_binance(self) -> Decimal:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise PriceFeedUnavailable(f"Binance API status {response.status}")

                payload = await response.json()
                return Decimal(str(payload["price"]))

    def _parse_timestamp(self, value: str) -> datetime:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")

        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _default_shared_file(self) -> str:
        env_path = os.getenv("CHARLIE_BTC_PRICE_FILE")
        if env_path:
            return env_path

        if os.name == "nt":
            return str(Path(tempfile.gettempdir()) / "btc_price.json")

        return "/tmp/btc_price.json"
