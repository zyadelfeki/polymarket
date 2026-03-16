from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

from data_feeds.external.base import ExternalAdapterBase


class CoinPaprikaAdapter(ExternalAdapterBase):
    provider_name = "coinpaprika"

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        if symbol.upper() != "BTC":
            raise ValueError(f"Coinpaprika adapter only supports BTC: {symbol}")
        return await self._request_json("https://api.coinpaprika.com/v1/tickers/btc-bitcoin")

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        quotes = payload.get("quotes") if isinstance(payload, dict) else None
        usd = quotes.get("USD") if isinstance(quotes, dict) else None
        if not isinstance(usd, dict):
            raise ValueError("coinpaprika payload missing quotes.USD")

        price = self._to_decimal(usd.get("price"))
        ath_price = self._to_decimal(usd.get("ath_price"))
        percent_from_price_ath = self._to_decimal(usd.get("percent_from_price_ath"))
        change_24h = self._to_decimal(usd.get("percent_change_24h"))

        if percent_from_price_ath is None and price is not None and ath_price and ath_price > 0:
            percent_from_price_ath = ((price - ath_price) / ath_price) * Decimal("100")

        return {
            "value": price,
            "ath_distance_pct": percent_from_price_ath,
            "change_24h_pct": change_24h,
        }
