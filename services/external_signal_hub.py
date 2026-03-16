from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from data_feeds.external.coingecko_adapter import CoinGeckoAdapter
from data_feeds.external.coincap_adapter import CoinCapAdapter
from data_feeds.external.coinpaprika_adapter import CoinPaprikaAdapter
from data_feeds.external.cryptocompare_adapter import CryptoCompareAdapter
from data_feeds.external.fred_adapter import FREDAdapter
from models.external_signals import ExternalSignalSnapshot, ProviderSignal

logger = logging.getLogger(__name__)


class ExternalSignalHub:
    """Builds a normalized snapshot from multiple external providers."""

    def __init__(self, config: Optional[dict] = None) -> None:
        cfg = config or {}

        retries = int(cfg.get("max_retries", 1))
        timeout = float(cfg.get("timeout_seconds", 2.5))

        self.price_divergence_bps_limit = Decimal(str(cfg.get("price_divergence_bps_limit", "80")))
        self.max_staleness_seconds = float(cfg.get("max_staleness_seconds", 90.0))

        self.adapters = {
            "coingecko": CoinGeckoAdapter(
                ttl_seconds=float(cfg.get("coingecko_ttl_seconds", 45.0)),
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "coincap": CoinCapAdapter(
                ttl_seconds=float(cfg.get("coincap_ttl_seconds", 20.0)),
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "coinpaprika": CoinPaprikaAdapter(
                ttl_seconds=float(cfg.get("coinpaprika_ttl_seconds", 90.0)),
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "cryptocompare": CryptoCompareAdapter(
                ttl_seconds=float(cfg.get("cryptocompare_ttl_seconds", 60.0)),
                timeout_seconds=timeout,
                max_retries=retries,
            ),
            "fred": FREDAdapter(
                ttl_seconds=float(cfg.get("fred_ttl_seconds", 3600.0)),
                timeout_seconds=float(cfg.get("fred_timeout_seconds", 4.0)),
                max_retries=retries,
            ),
        }

    async def get_snapshot(
        self,
        *,
        symbol: str = "BTC",
        btc_spot_binance: Optional[Decimal] = None,
    ) -> ExternalSignalSnapshot:
        providers: Dict[str, ProviderSignal] = {}
        for name, adapter in self.adapters.items():
            providers[name] = await adapter.fetch(symbol=symbol)

        alt_candidates = [
            providers["coincap"].value,
            providers["coingecko"].value,
            providers["coinpaprika"].value,
        ]
        btc_spot_alt = next((v for v in alt_candidates if v is not None), None)
        btc_oracle_proxy = providers["coingecko"].value or providers["coinpaprika"].value

        social_score = Decimal("0")
        social_raw = providers["cryptocompare"].extras.get("social_momentum_z")
        if social_raw is not None:
            social_score = Decimal(str(social_raw))

        macro_score = Decimal("0")
        macro_raw = providers["fred"].extras.get("macro_risk_score")
        if macro_raw is not None:
            macro_score = Decimal(str(macro_raw))

        news_score = Decimal("0")

        stale_provider_count = 0
        healthy_provider_count = 0
        health_flags = []
        for name, signal in providers.items():
            stale = signal.staleness_seconds > self.max_staleness_seconds
            if stale:
                stale_provider_count += 1
                health_flags.append(f"{name}_stale")
            if signal.provider_ok and not stale:
                healthy_provider_count += 1
            if not signal.provider_ok:
                reason = signal.degraded_reason or "provider_not_ok"
                health_flags.append(f"{name}_{reason}")

        provider_health_score = Decimal("0")
        total = len(providers)
        if total > 0:
            provider_health_score = Decimal(str(healthy_provider_count / total))

        divergence_bps: Optional[Decimal] = None
        price_truth_degraded = False
        if btc_spot_binance is not None and btc_spot_alt is not None and btc_spot_binance > 0:
            divergence_bps = ((btc_spot_alt - btc_spot_binance).copy_abs() / btc_spot_binance) * Decimal("10000")
            if divergence_bps > self.price_divergence_bps_limit:
                price_truth_degraded = True
                health_flags.append("price_truth_degraded")
        elif btc_spot_binance is not None and btc_spot_alt is None:
            price_truth_degraded = True
            health_flags.append("price_truth_missing_alt")

        return ExternalSignalSnapshot(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol.upper(),
            btc_spot_binance=btc_spot_binance,
            btc_spot_alt=btc_spot_alt,
            btc_oracle_proxy=btc_oracle_proxy,
            social_score=social_score,
            macro_score=macro_score,
            news_score=news_score,
            provider_health_score=provider_health_score,
            btc_cross_source_divergence_bps=divergence_bps,
            price_truth_degraded=price_truth_degraded,
            stale_provider_count=stale_provider_count,
            healthy_provider_count=healthy_provider_count,
            providers=providers,
            health_flags=health_flags,
        )
