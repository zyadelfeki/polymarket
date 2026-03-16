from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict

from data_feeds.external.base import ExternalAdapterBase


class FREDAdapter(ExternalAdapterBase):
    provider_name = "fred"

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        _ = symbol
        api_key = os.getenv("FRED_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("FRED_API_KEY missing")

        # DEXUSEU (USD index proxy) is daily. Keep this adapter low-frequency.
        return await self._request_json(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DTWEXBGS",  # Trade-weighted USD index
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": 3,
            },
        )

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        observations = payload.get("observations") if isinstance(payload, dict) else None
        if not isinstance(observations, list) or len(observations) < 2:
            raise ValueError("fred payload missing observations")

        latest = observations[0].get("value")
        previous = observations[1].get("value")
        latest_dec = self._to_decimal(latest)
        prev_dec = self._to_decimal(previous)
        if latest_dec is None or prev_dec is None or prev_dec == 0:
            raise ValueError("fred observations missing numeric values")

        pct_change = ((latest_dec - prev_dec) / prev_dec) * Decimal("100")
        macro_risk_score = max(Decimal("-3"), min(Decimal("3"), pct_change * Decimal("5")))

        return {
            "value": latest_dec,
            "macro_risk_score": macro_risk_score,
            "dxy_pct_change": pct_change,
        }
