from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderSignal:
    provider: str
    value: Optional[Decimal]
    staleness_seconds: float
    provider_ok: bool
    degraded_reason: Optional[str]
    observed_at: datetime
    extras: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalSignalSnapshot:
    timestamp: datetime
    symbol: str
    btc_spot_binance: Optional[Decimal]
    btc_spot_alt: Optional[Decimal]
    btc_oracle_proxy: Optional[Decimal]
    social_score: Decimal
    macro_score: Decimal
    news_score: Decimal
    provider_health_score: Decimal
    btc_cross_source_divergence_bps: Optional[Decimal]
    price_truth_degraded: bool
    stale_provider_count: int
    healthy_provider_count: int
    providers: Dict[str, ProviderSignal]
    health_flags: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AdmissionVerdict:
    allowed: bool
    confidence_multiplier: Decimal
    size_multiplier: Decimal
    block_reason: Optional[str]
    health_flags: List[str] = field(default_factory=list)
