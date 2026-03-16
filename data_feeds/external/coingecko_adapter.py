from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from data_feeds.external.base import ExternalAdapterBase


class CoinGeckoAdapter(ExternalAdapterBase):
    provider_name = "coingecko"

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        if symbol.upper() != "BTC":
            raise ValueError(f"CoinGecko adapter only supports BTC: {symbol}")
        return await self._request_json(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": "bitcoin",
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
                "include_market_cap_change_percentage_24h": "true",
            },
        )

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        btc = payload.get("bitcoin") if isinstance(payload, dict) else None
        if not isinstance(btc, dict):
            raise ValueError("coingecko payload missing bitcoin object")

        price = self._to_decimal(btc.get("usd"))
        vol_24h = self._to_decimal(btc.get("usd_24h_vol"))
        price_change_24h = self._to_decimal(btc.get("usd_24h_change"))
        mcap_change_24h = self._to_decimal(btc.get("usd_market_cap_change_24h"))

        breadth_score = Decimal("0")
        if mcap_change_24h is not None:
            breadth_score += max(Decimal("-3"), min(Decimal("3"), mcap_change_24h / Decimal("3")))
        if price_change_24h is not None:
            breadth_score += max(Decimal("-2"), min(Decimal("2"), price_change_24h / Decimal("2")))

        return {
            "value": price,
            "volume_24h_usd": vol_24h,
            "price_change_24h_pct": price_change_24h,
            "market_cap_change_24h_pct": mcap_change_24h,
            "breadth_score": breadth_score,
        }
