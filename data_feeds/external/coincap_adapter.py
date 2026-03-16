from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from data_feeds.external.base import ExternalAdapterBase


class CoinCapAdapter(ExternalAdapterBase):
    provider_name = "coincap"

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        if symbol.upper() != "BTC":
            raise ValueError(f"CoinCap adapter only supports BTC: {symbol}")
        return await self._request_json("https://api.coincap.io/v2/assets/bitcoin")

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("coincap payload missing data object")

        price = self._to_decimal(data.get("priceUsd"))
        volume_24h = self._to_decimal(data.get("volumeUsd24Hr"))
        change_24h = self._to_decimal(data.get("changePercent24Hr"))

        market_breadth_score = Decimal("0")
        if change_24h is not None:
            market_breadth_score = max(Decimal("-3"), min(Decimal("3"), change_24h / Decimal("2")))

        return {
            "value": price,
            "volume_24h_usd": volume_24h,
            "change_24h_pct": change_24h,
            "breadth_score": market_breadth_score,
        }
