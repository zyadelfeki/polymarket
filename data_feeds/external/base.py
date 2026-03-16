from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx

from models.external_signals import ProviderSignal

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    payload: Dict[str, Any]
    observed_at: datetime
    expires_at_monotonic: float


class ExternalAdapterBase:
    provider_name: str = "base"

    def __init__(
        self,
        *,
        ttl_seconds: float,
        timeout_seconds: float,
        max_retries: int = 1,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self._cache: Optional[_CacheEntry] = None
        self._consecutive_failures = 0
        self._circuit_until_monotonic = 0.0

    async def fetch(self, symbol: str = "BTC") -> ProviderSignal:
        now_mono = time.monotonic()
        if self._cache is not None and self._cache.expires_at_monotonic > now_mono:
            return self._to_provider_signal(self._cache.payload, self._cache.observed_at, from_cache=True)

        if self._circuit_until_monotonic > now_mono and self._cache is not None:
            signal = self._to_provider_signal(self._cache.payload, self._cache.observed_at, from_cache=True)
            return ProviderSignal(
                provider=signal.provider,
                value=signal.value,
                staleness_seconds=signal.staleness_seconds,
                provider_ok=False,
                degraded_reason="provider_circuit_open",
                observed_at=signal.observed_at,
                extras=signal.extras,
            )

        try:
            payload = await self._fetch_payload(symbol)
            observed_at = datetime.now(timezone.utc)
            self._cache = _CacheEntry(
                payload=payload,
                observed_at=observed_at,
                expires_at_monotonic=time.monotonic() + self.ttl_seconds,
            )
            self._consecutive_failures = 0
            self._circuit_until_monotonic = 0.0
            return self._to_provider_signal(payload, observed_at, from_cache=False)
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._circuit_until_monotonic = time.monotonic() + min(self.ttl_seconds, 30.0)

            if self._cache is not None:
                fallback = self._to_provider_signal(self._cache.payload, self._cache.observed_at, from_cache=True)
                return ProviderSignal(
                    provider=fallback.provider,
                    value=fallback.value,
                    staleness_seconds=fallback.staleness_seconds,
                    provider_ok=False,
                    degraded_reason=f"fetch_failed:{type(exc).__name__}",
                    observed_at=fallback.observed_at,
                    extras=fallback.extras,
                )

            return ProviderSignal(
                provider=self.provider_name,
                value=None,
                staleness_seconds=float("inf"),
                provider_ok=False,
                degraded_reason=f"fetch_failed:{type(exc).__name__}",
                observed_at=datetime.now(timezone.utc),
                extras={},
            )

    async def _fetch_payload(self, symbol: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def _to_provider_signal(self, payload: Dict[str, Any], observed_at: datetime, *, from_cache: bool) -> ProviderSignal:
        normalized = self._normalize_payload(payload)
        staleness_seconds = max(0.0, (datetime.now(timezone.utc) - observed_at).total_seconds())
        degraded_reason = None if not from_cache else "cache_fallback"
        return ProviderSignal(
            provider=self.provider_name,
            value=normalized.get("value"),
            staleness_seconds=staleness_seconds,
            provider_ok=True,
            degraded_reason=degraded_reason,
            observed_at=observed_at,
            extras=normalized,
        )

    async def _request_json(self, url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                timeout = httpx.Timeout(self.timeout_seconds)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(url, params=params, headers=headers)
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(0.2 * (attempt + 1))

        raise RuntimeError(f"{self.provider_name} request failed: {last_error}")

    @staticmethod
    def _to_decimal(value: Any) -> Optional[Decimal]:
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None
