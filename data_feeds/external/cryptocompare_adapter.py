from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from data_feeds.external.base import ExternalAdapterBase


class CryptoCompareAdapter(ExternalAdapterBase):
    provider_name = "cryptocompare"

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        if symbol.upper() != "BTC":
            raise ValueError(f"CryptoCompare adapter only supports BTC: {symbol}")

        # Public endpoint; no API key required for basic usage.
        return await self._request_json(
            "https://min-api.cryptocompare.com/data/v2/social/coin/latest",
            params={"coinId": 1182},
        )

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = payload.get("Data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError("cryptocompare payload missing Data")

        points = Decimal(str(data.get("Points", 0) or 0))
        followers = Decimal(str(data.get("Twitter", {}).get("followers", 0) or 0))
        reddit_subscribers = Decimal(str(data.get("Reddit", {}).get("subscribers", 0) or 0))

        # Simple bounded proxy score in [-3, 3].
        raw = (points / Decimal("200")) + (followers / Decimal("500000")) + (reddit_subscribers / Decimal("500000"))
        social_momentum_z = max(Decimal("-3"), min(Decimal("3"), raw))

        return {
            "value": None,
            "social_momentum_z": social_momentum_z,
            "raw_points": points,
            "twitter_followers": followers,
            "reddit_subscribers": reddit_subscribers,
        }
