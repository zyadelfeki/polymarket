from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from utils.decimal_helpers import to_decimal
from data_feeds.binance_features import get_all_features as _get_binance_features


class _LoggerShim:
    """Mirrors latency_arbitrage_btc._LoggerShim — structlog-style calls via stdlib."""

    def __init__(self, base: logging.Logger) -> None:
        self._base = base

    def debug(self, event: str, **kwargs) -> None:
        self._base.debug("%s %s", event, kwargs or "")

    def info(self, event: str, **kwargs) -> None:
        self._base.info("%s %s", event, kwargs or "")

    def warning(self, event: str, **kwargs) -> None:
        self._base.warning("%s %s", event, kwargs or "")

    def error(self, event: str, **kwargs) -> None:
        self._base.error("%s %s", event, kwargs or "")


logger = _LoggerShim(logging.getLogger(__name__))


def _decimal_from_charlie(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class BTCPriceLevelScanner:
    """Find BTC price-level markets that Charlie already considers tradable."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.max_days_to_expiry = int(cfg.get("max_days_to_expiry", 7))
        self.min_edge = to_decimal(cfg.get("min_edge", "0.05"))
        self.market_limit = int(cfg.get("market_limit", 200))
        self.default_timeframe = str(cfg.get("timeframe", "15m"))

    async def scan(
        self,
        charlie_gate,
        api_client,
        equity: Decimal,
        max_days_to_expiry: int = 7,
    ) -> List[Dict[str, Any]]:
        if not self.enabled or charlie_gate is None or api_client is None:
            logger.warning(
                "btc_scanner_skip",
                reason="disabled_or_missing_deps",
                enabled=self.enabled,
                charlie_gate_present=(charlie_gate is not None),
                api_client_present=(api_client is not None),
            )
            return []

        expiry_window_days = max_days_to_expiry or self.max_days_to_expiry
        markets = await self._fetch_markets(api_client)
        logger.info("btc_scanner_fetch_complete", total_markets=len(markets))
        if not markets:
            logger.warning("btc_scanner_no_markets", reason="empty_market_list_from_api")
            return []

        btc_extra_features = _get_binance_features("BTC")
        logger.info(
            "btc_scanner_binance_features_fetched",
            features_available=(btc_extra_features is not None),
            feature_keys=list(btc_extra_features.keys()) if btc_extra_features else [],
        )

        after_price_level_filter = 0
        after_expiry_filter = 0
        after_id_question_filter = 0
        after_price_fetch = 0
        charlie_none_count = 0
        edge_too_low_count = 0

        opportunities: List[Dict[str, Any]] = []
        for market in markets:
            if not isinstance(market, dict):
                continue
            if not self._looks_like_price_level_market(market):
                continue
            after_price_level_filter += 1

            if not self._resolves_within_window(market, expiry_window_days):
                continue
            after_expiry_filter += 1

            market_id = str(market.get("id") or market.get("condition_id") or market.get("market_id") or "").strip()
            question = str(market.get("question") or market.get("title") or "").strip()
            if not market_id or not question:
                continue
            after_id_question_filter += 1

            market_price = self._extract_market_price(market)
            if market_price is None:
                market_price = await self._fetch_market_price(api_client, market_id)
            if market_price is None:
                logger.debug("btc_scanner_market_skip", reason="no_price", market_id=market_id)
                continue
            after_price_fetch += 1

            recommendation = await charlie_gate.evaluate_market(
                market_id=market_id,
                market_price=market_price,
                symbol="BTC",
                timeframe=self.default_timeframe,
                bankroll=equity,
                market_question=question,
                extra_features=btc_extra_features,
            )
            if recommendation is None:
                charlie_none_count += 1
                logger.info(
                    "btc_scanner_charlie_rejected",
                    market_id=market_id,
                    question=question[:80],
                    hint="check charlie_coin_flip_rejected or edge_below_threshold in charlie logs",
                )
                continue

            edge = _decimal_from_charlie(recommendation.edge)
            if edge < self.min_edge:
                edge_too_low_count += 1
                logger.info(
                    "btc_scanner_edge_too_low",
                    market_id=market_id,
                    edge=str(edge),
                    min_edge=str(self.min_edge),
                )
                continue

            opportunities.append(self._build_opportunity(market, recommendation, market_price, question))

        logger.info(
            "btc_scanner_complete",
            total_fetched=len(markets),
            after_price_level_filter=after_price_level_filter,
            after_expiry_filter=after_expiry_filter,
            after_id_question_filter=after_id_question_filter,
            after_price_fetch=after_price_fetch,
            charlie_none_count=charlie_none_count,
            edge_too_low_count=edge_too_low_count,
            opportunities_found=len(opportunities),
        )

        opportunities.sort(key=lambda item: to_decimal(item.get("edge", "0")), reverse=True)
        return opportunities

    async def _fetch_markets(self, api_client) -> List[Dict[str, Any]]:
        if hasattr(api_client, "get_markets"):
            markets = await api_client.get_markets(active=True, limit=self.market_limit)
            if markets:
                return [m for m in markets if isinstance(m, dict)]
        if hasattr(api_client, "get_active_markets"):
            markets = await api_client.get_active_markets(limit=self.market_limit)
            if markets:
                return [m for m in markets if isinstance(m, dict)]
        return []

    async def _fetch_market_price(self, api_client, market_id: str) -> Optional[Decimal]:
        if not hasattr(api_client, "get_market_orderbook_summary"):
            return None
        summary = await api_client.get_market_orderbook_summary(market_id)
        if not isinstance(summary, dict):
            return None

        bid = self._coerce_optional_decimal(summary.get("bid"))
        ask = self._coerce_optional_decimal(summary.get("ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / Decimal("2")
        return ask or bid

    def _looks_like_price_level_market(self, market: Dict[str, Any]) -> bool:
        question = str(market.get("question") or market.get("title") or "").lower()
        slug = str(market.get("slug") or "").lower()

        has_price_language = any(
            token in question or token in slug
            for token in ("price", "$", "exceed", "above", "reach", "hit")
        )
        has_crypto_language = any(
            token in question or token in slug
            for token in ("btc", "bitcoin")
        )
        return has_price_language and has_crypto_language

    def _resolves_within_window(self, market: Dict[str, Any], max_days_to_expiry: int) -> bool:
        end_dt = self._extract_market_datetime(market)
        if end_dt is None:
            return False
        now = datetime.now(timezone.utc)
        return now <= end_dt <= (now + timedelta(days=max_days_to_expiry))

    def _extract_market_datetime(self, market: Dict[str, Any]) -> Optional[datetime]:
        for key in ("end_date", "endDate", "resolution_date", "resolve_date", "closedTime"):
            raw_value = market.get(key)
            if not raw_value:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    def _extract_market_price(self, market: Dict[str, Any]) -> Optional[Decimal]:
        tokens = market.get("tokens") or []
        if not isinstance(tokens, list):
            return None
        for token in tokens:
            if not isinstance(token, dict):
                continue
            outcome = str(token.get("outcome") or token.get("name") or "").strip().lower()
            if outcome not in {"yes", "up"}:
                continue
            for key in ("price", "last_price", "lastPrice", "probability"):
                value = self._coerce_optional_decimal(token.get(key))
                if value is not None:
                    return value
        return None

    def _build_opportunity(self, market: Dict[str, Any], recommendation, market_price: Decimal, question: str) -> Dict[str, Any]:
        side = str(recommendation.side).upper()
        token_id = self._extract_token_id(market, side)
        direction = "UP" if side == "YES" else "DOWN"
        confidence = _decimal_from_charlie(recommendation.confidence)
        true_prob = _decimal_from_charlie(recommendation.p_win_calibrated)

        return {
            "market_id": str(market.get("id") or market.get("condition_id") or market.get("market_id")),
            "token_id": token_id,
            "side": side,
            "outcome": direction,
            "asset": "BTC",
            "true_prob": true_prob,
            "market_price": market_price,
            "edge": _decimal_from_charlie(recommendation.edge),
            "raw_edge": _decimal_from_charlie(recommendation.edge),
            "confidence": confidence,
            "charlie_confidence": confidence,
            "direction": direction,
            "btc_price": None,
            "asset_price": None,
            "start_price": None,
            "price_change_pct": None,
            "question": question,
            "timeframe": self.default_timeframe,
            "size": _decimal_from_charlie(recommendation.size),
            "kelly_fraction": _decimal_from_charlie(recommendation.kelly_fraction),
            "implied_prob": _decimal_from_charlie(recommendation.implied_prob),
            "technical_regime": str(recommendation.technical_regime),
            "reason": str(recommendation.reason),
            "model_votes": recommendation.model_votes,
            "ofi_conflict": bool(recommendation.ofi_conflict),
        }

    def _extract_token_id(self, market: Dict[str, Any], side: str) -> Optional[str]:
        target_outcomes = {"yes", "up"} if side == "YES" else {"no", "down"}
        tokens = market.get("tokens") or []
        for token in tokens:
            if not isinstance(token, dict):
                continue
            outcome = str(token.get("outcome") or token.get("name") or "").strip().lower()
            if outcome in target_outcomes:
                token_id = token.get("token_id") or token.get("id")
                if token_id:
                    return str(token_id)
        return None

    def _coerce_optional_decimal(self, value: Any) -> Optional[Decimal]:
        if value in (None, ""):
            return None
        try:
            return to_decimal(value)
        except Exception:
            return None