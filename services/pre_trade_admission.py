from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from models.external_signals import AdmissionVerdict
from services.external_signal_hub import ExternalSignalHub

logger = logging.getLogger(__name__)


class PreTradeAdmissionGate:
    """Shared pre-trade gate for all strategy paths."""

    def __init__(self, signal_hub: ExternalSignalHub, config: Optional[dict] = None) -> None:
        cfg = config or {}
        self.signal_hub = signal_hub
        self.enabled = bool(cfg.get("enabled", True))
        self.min_healthy_providers = int(cfg.get("min_healthy_providers", 2))
        self.max_price_divergence_bps = Decimal(str(cfg.get("max_price_divergence_bps", "90")))
        self.macro_risk_block_threshold = Decimal(str(cfg.get("macro_risk_block_threshold", "2.0")))
        self.near_expiry_block_minutes = int(cfg.get("near_expiry_block_minutes", 45))

    async def evaluate(
        self,
        *,
        opportunity: Dict[str, Any],
        market_price: Decimal,
        side: str,
        trade_confidence: Decimal,
    ) -> AdmissionVerdict:
        _ = market_price
        _ = side
        _ = trade_confidence

        if not self.enabled:
            return AdmissionVerdict(
                allowed=True,
                confidence_multiplier=Decimal("1"),
                size_multiplier=Decimal("1"),
                block_reason=None,
                health_flags=["external_admission_disabled"],
            )

        binance_price = self._extract_binance_price(opportunity)
        snapshot = await self.signal_hub.get_snapshot(symbol="BTC", btc_spot_binance=binance_price)

        if snapshot.healthy_provider_count < self.min_healthy_providers:
            return AdmissionVerdict(
                allowed=False,
                confidence_multiplier=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="provider_quorum_failed",
                health_flags=snapshot.health_flags,
            )

        if snapshot.btc_cross_source_divergence_bps is not None and snapshot.btc_cross_source_divergence_bps > self.max_price_divergence_bps:
            return AdmissionVerdict(
                allowed=False,
                confidence_multiplier=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="cross_source_price_divergence",
                health_flags=snapshot.health_flags,
            )

        if snapshot.macro_score >= self.macro_risk_block_threshold:
            return AdmissionVerdict(
                allowed=False,
                confidence_multiplier=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="macro_risk_extreme",
                health_flags=snapshot.health_flags,
            )

        minutes_to_expiry = self._minutes_to_expiry(opportunity)
        if minutes_to_expiry is not None and minutes_to_expiry <= self.near_expiry_block_minutes and snapshot.price_truth_degraded:
            return AdmissionVerdict(
                allowed=False,
                confidence_multiplier=Decimal("0"),
                size_multiplier=Decimal("0"),
                block_reason="near_expiry_with_degraded_price_truth",
                health_flags=snapshot.health_flags,
            )

        health = max(Decimal("0.25"), min(Decimal("1.0"), snapshot.provider_health_score))
        confidence_multiplier = health
        size_multiplier = max(Decimal("0.35"), min(Decimal("1.0"), health))

        return AdmissionVerdict(
            allowed=True,
            confidence_multiplier=confidence_multiplier,
            size_multiplier=size_multiplier,
            block_reason=None,
            health_flags=snapshot.health_flags,
        )

    def _extract_binance_price(self, opportunity: Dict[str, Any]) -> Optional[Decimal]:
        for key in ("btc_price", "asset_price", "start_price"):
            value = opportunity.get(key)
            if value is None:
                continue
            try:
                parsed = Decimal(str(value))
                if parsed > 0:
                    return parsed
            except Exception:
                continue
        return None

    def _minutes_to_expiry(self, opportunity: Dict[str, Any]) -> Optional[int]:
        if "minutes_to_expiry" in opportunity:
            try:
                return int(opportunity["minutes_to_expiry"])
            except Exception:
                return None

        for key in ("end_date", "endDate", "resolution_date", "resolve_date", "closedTime"):
            raw_value = opportunity.get(key)
            if not raw_value:
                continue
            try:
                end_dt = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
            except Exception:
                continue
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return int((end_dt - now).total_seconds() / 60)

        return None
