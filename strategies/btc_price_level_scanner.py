from __future__ import annotations

import asyncio
import logging
import time
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
        self._recent_rejection_ttl_seconds = int(cfg.get("recent_rejection_ttl_seconds", 120))
        self._expired_market_ids: set[str] = set()
        self._closed_market_ids: set[str] = set()
        self._permanent_rejection_cache: set[str] = set()
        self._recent_rejection_cache: Dict[tuple[str, str], float] = {}

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

        # --- Regime guard (once per cycle, cached 2 min) ----------------------
        _verdict = None
        try:
            from ai.regime_guard import get_regime_verdict
            _btc_price = float(btc_extra_features.get("price", 0)) if btc_extra_features else 0
            _rsi = float(btc_extra_features.get("rsi_14", 50)) if btc_extra_features else 50
            _price_1h = float(btc_extra_features.get("price_change_1h", 0)) if btc_extra_features else 0
            _atr = float(btc_extra_features.get("atr_pct", 1.0)) if btc_extra_features else 1.0
            _verdict = await get_regime_verdict(
                _btc_price, _rsi, _price_1h, _atr,
                open_positions=len(opportunities) if "opportunities" in dir() else 0,
            )
            if not _verdict.safe_to_trade:
                logger.warning(
                    "regime_guard_suppressed_scan",
                    regime=_verdict.regime_label,
                    confidence=_verdict.confidence,
                    reason=_verdict.reason,
                )
                return []
            logger.info(
                "regime_guard_passed",
                regime=_verdict.regime_label,
                source=_verdict.source,
            )
        except ImportError:
            pass

        after_price_level_filter = 0
        after_expiry_filter = 0
        after_id_question_filter = 0
        after_price_fetch = 0
        charlie_none_count = 0
        edge_too_low_count = 0

        opportunities: List[Dict[str, Any]] = []
        # Temporary: log cache sizes once per scan at start of loop
        logger.debug(
            "btc_scanner_cache_state",
            permanent_rejections=len(self._permanent_rejection_cache),
            recent_rejections=len(self._recent_rejection_cache),
            expired_markets=len(self._expired_market_ids),
            closed_markets=len(self._closed_market_ids),
        )
        for market in markets:
            if not isinstance(market, dict):
                continue

            market_id = str(market.get("id") or market.get("condition_id") or market.get("market_id") or "").strip()
            if not market_id:
                continue

            if market_id in self._expired_market_ids:
                continue
            if market_id in self._closed_market_ids:
                continue
            if market_id in self._permanent_rejection_cache:
                continue
            if self._is_recently_rejected(market_id):
                continue

            if self._is_market_closed(market):
                self._closed_market_ids.add(market_id)
                self._mark_recent_rejection(market_id, "market_closed", ttl_seconds=600)
                continue

            if not self._looks_like_price_level_market(market):
                continue
            after_price_level_filter += 1

            end_dt = self._extract_market_datetime(market)
            if end_dt is None:
                self._mark_recent_rejection(market_id, "missing_expiry", ttl_seconds=300)
                continue
            now = datetime.now(timezone.utc)
            if end_dt < now:
                self._expired_market_ids.add(market_id)
                self._mark_recent_rejection(market_id, "expired", ttl_seconds=600)
                continue
            if end_dt > (now + timedelta(days=expiry_window_days)):
                continue
            after_expiry_filter += 1

            question = str(market.get("question") or market.get("title") or "").strip()
            if not market_id or not question:
                self._mark_recent_rejection(market_id, "missing_question", ttl_seconds=120)
                continue
            after_id_question_filter += 1

            time_left_seconds = self._extract_time_left_seconds(market)
            if time_left_seconds is not None and time_left_seconds <= 0:
                self._expired_market_ids.add(market_id)
                self._mark_recent_rejection(market_id, "too_close_to_expiry", ttl_seconds=600)
                logger.info(
                    "btc_scanner_market_skip",
                    reason="too_close_to_expiry",
                    market_id=market_id,
                    time_left_seconds=time_left_seconds,
                    marked_expired=True,
                )
                continue

            if self._is_permanent_timeframe_mismatch(market):
                self._mark_permanent_rejection(market_id)
                self._mark_recent_rejection(
                    market_id,
                    "no_timeframe_match_from_metadata_or_question",
                    ttl_seconds=3600,
                )
                continue

            market_price = self._extract_market_price(market)
            if market_price is None:
                market_price = await self._fetch_market_price(api_client, market_id)
            if market_price is None:
                self._mark_recent_rejection(market_id, "no_price", ttl_seconds=90)
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
                self._mark_recent_rejection(market_id, "charlie_rejected", ttl_seconds=60)
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
                self._mark_recent_rejection(market_id, "edge_too_low", ttl_seconds=60)
                logger.info(
                    "btc_scanner_edge_too_low",
                    market_id=market_id,
                    edge=str(edge),
                    min_edge=str(self.min_edge),
                )
                continue

            # --- LLM layer: zero-latency cache read ----------------------------
            # The background LLMWorker fills the cache at the model's own pace
            # (~9 s per call).  The scanner only reads the cache — O(1), no
            # blocking.  A cache miss is a silent pass-through; trading is never
            # gated on LLM availability.
            _cached = None
            try:
                from ai.llm_worker import get_cache as _get_llm_cache
                _cached = await _get_llm_cache().get(market_id, question)
                if _cached is not None:
                    _anomaly = _cached.get("anomaly")
                    _coherence = _cached.get("coherence")
                    if _anomaly:
                        logger.warning(
                            "llm_anomaly_veto",
                            market_id=market_id,
                            question=question[:80],
                        )
                        charlie_none_count += 1
                        continue
                    if _coherence and _coherence.vetoed:
                        logger.warning(
                            "llm_coherence_veto",
                            market_id=market_id,
                            reason=_coherence.reason,
                            confidence=_coherence.confidence,
                        )
                        charlie_none_count += 1
                        continue
                    _eq = _cached.get("edge_quality")
                    logger.info(
                        "llm_cache_hit_passed",
                        market_id=market_id,
                        coherent=(_coherence.coherent if _coherence else None),
                        edge_quality_score=(_eq.score if _eq else None),
                        edge_quality_flags=(_eq.flags if _eq else None),
                    )
            except Exception as _llm_err:
                logger.warning("llm_layer_error", error=str(_llm_err), market_id=market_id)

            opportunities.append(
                self._build_opportunity(
                    market,
                    recommendation,
                    market_price,
                    question,
                    btc_extra_features=btc_extra_features,
                )
            )

            # Enqueue for background LLM inference (non-blocking, fire-and-forget).
            # The worker will update the cache for the *next* scanner pass.
            try:
                import ai.llm_worker as _llm_worker_mod
                if _llm_worker_mod._singleton_worker is not None:
                    _btc_price_enq = (
                        float(btc_extra_features.get("price", 0))
                        if btc_extra_features else 0
                    )
                    _end_dt_enq = self._extract_market_datetime(market)
                    if _end_dt_enq:
                        _minutes_expiry = int(
                            (_end_dt_enq - datetime.now(timezone.utc)).total_seconds() / 60
                        )
                    else:
                        logger.debug("minutes_to_expiry_unavailable", market_id=market_id)
                        _minutes_expiry = 30
                    _llm_worker_mod._singleton_worker.enqueue([{
                        "market_id":        market_id,
                        "question":         question,
                        "market_price":     float(market_price),
                        "btc_price":        _btc_price_enq,
                        "rsi":              float(btc_extra_features.get("rsi_14", 50)) if btc_extra_features else 50,
                        "macd":             float(btc_extra_features.get("macd", 0)) if btc_extra_features else 0,
                        "charlie_side":     recommendation.side,
                        "p_win":            float(recommendation.p_win),
                        "edge":             float(recommendation.edge),
                        "confidence":       float(recommendation.confidence),
                        "strike":           0,
                        "minutes_to_expiry": _minutes_expiry,
                    }])
            except Exception:
                pass  # worker unavailable — never block trading

            # Fire-and-forget feedback log entry.
            try:
                from ai.feedback_loop import record_decision
                asyncio.create_task(record_decision(
                    market_id=market_id,
                    question=question,
                    charlie_side=str(recommendation.side),
                    p_win=float(recommendation.p_win),
                    edge=float(recommendation.edge),
                    llm_coherent=(
                        _cached.get("coherence").coherent
                        if _cached and _cached.get("coherence") else None
                    ),
                    llm_coherence_confidence=(
                        _cached.get("coherence").confidence
                        if _cached and _cached.get("coherence") else None
                    ),
                    llm_is_trap=(_cached.get("anomaly") if _cached else None),
                    llm_trap_confidence=None,
                    edge_quality_score=(
                        _cached.get("edge_quality").score
                        if _cached and _cached.get("edge_quality") else None
                    ),
                    regime_label=(_verdict.regime_label if _verdict is not None else "UNKNOWN"),
                    action="APPROVED",
                ))
            except Exception:
                pass

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

    def _build_opportunity(
        self,
        market: Dict[str, Any],
        recommendation,
        market_price: Decimal,
        question: str,
        btc_extra_features: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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
            "btc_extra_features": btc_extra_features or {},
            "btc_price": float(btc_extra_features.get("price", 0)) if btc_extra_features else None,
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

    def _is_market_closed(self, market: Dict[str, Any]) -> bool:
        status = str(market.get("status") or market.get("state") or "").strip().lower()
        if status in {"closed", "resolved", "settled", "finalized", "ended"}:
            return True
        active = market.get("active")
        if isinstance(active, bool) and not active:
            return True
        return False

    def _is_recently_rejected(self, market_id: str) -> bool:
        now_mono = time.monotonic()
        stale_keys = [
            key
            for key, expires_at in self._recent_rejection_cache.items()
            if expires_at <= now_mono
        ]
        for key in stale_keys:
            self._recent_rejection_cache.pop(key, None)

        for (cached_market_id, _reason), expires_at in self._recent_rejection_cache.items():
            if cached_market_id == market_id and expires_at > now_mono:
                return True
        return False

    def _mark_recent_rejection(self, market_id: str, reason: str, ttl_seconds: Optional[int] = None) -> None:
        ttl = int(ttl_seconds if ttl_seconds is not None else self._recent_rejection_ttl_seconds)
        self._recent_rejection_cache[(market_id, reason)] = time.monotonic() + max(1, ttl)

    def _mark_permanent_rejection(self, market_id: str) -> None:
        self._permanent_rejection_cache.add(market_id)

    def _is_permanent_timeframe_mismatch(self, market: Dict[str, Any]) -> bool:
        question = str(market.get("question") or market.get("title") or "").lower()
        slug = str(market.get("slug") or "").lower()
        resolution_raw = (
            market.get("resolution")
            or market.get("rules")
            or market.get("resolution_time")
            or market.get("resolutionTime")
            or ""
        )
        resolution_text = str(resolution_raw).lower()

        # Any known intraday or daily timeframe hint means this market is not permanent-mismatch.
        timeframe_tokens = (
            "15m",
            "15 min",
            "15-minute",
            "1h",
            "1 hour",
            "hourly",
            "4h",
            "4 hour",
            "4-hour",
            "daily",
            "1d",
            "day",
            "intraday",
            "updown-daily",
            "updown-4h",
            "updown-1h",
            "updown-15m",
        )
        combined = " ".join((question, slug, resolution_text))
        if any(token in combined for token in timeframe_tokens):
            return False

        duration_seconds = self._extract_duration_seconds(market)
        if duration_seconds is not None:
            if 12 * 60 <= duration_seconds <= 18 * 60:
                return False
            if 45 * 60 <= duration_seconds <= int(2.5 * 3600):
                return False
            if int(3.0 * 3600) <= duration_seconds <= int(6.0 * 3600):
                return False
            if int(18 * 3600) <= duration_seconds <= int(36 * 3600):
                return False

        time_left_seconds = self._extract_time_left_seconds(market)
        if time_left_seconds is not None:
            if 30 * 60 <= time_left_seconds <= int(6.5 * 3600):
                return False

        end_dt = self._extract_market_datetime(market)
        if end_dt is not None:
            now_utc = datetime.now(timezone.utc)
            day_delta = (end_dt.date() - now_utc.date()).days
            if day_delta in {0, 1}:
                return False

        return True

    def _extract_duration_seconds(self, market: Dict[str, Any]) -> Optional[int]:
        start_dt = self._extract_market_datetime_field(
            market,
            [
                "game_start_time",
                "gameStartTime",
                "startDate",
                "start_date",
                "startTime",
                "start_time",
                "open_time",
                "openTime",
                "created_at",
                "createdAt",
            ],
        )
        end_dt = self._extract_market_datetime_field(
            market,
            [
                "end_date_iso",
                "endDateIso",
                "endDateISO",
                "endDate",
                "end_date",
                "endTime",
                "end_time",
                "closeTime",
                "close_time",
                "closes_at",
                "resolve_time",
                "resolution_time",
                "resolutionTime",
                "expires_at",
                "expiresAt",
            ],
        )
        if not start_dt or not end_dt:
            return None
        return int((end_dt - start_dt).total_seconds())

    def _extract_market_datetime_field(self, market: Dict[str, Any], fields: List[str]) -> Optional[datetime]:
        for field in fields:
            raw_value = market.get(field)
            if raw_value in (None, ""):
                continue
            try:
                parsed = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    def _extract_time_left_seconds(self, market: Dict[str, Any]) -> Optional[int]:
        for key in ("time_left_seconds", "timeLeftSeconds", "seconds_to_expiry", "secondsToExpiry"):
            raw = market.get(key)
            if raw in (None, ""):
                continue
            try:
                value = int(float(raw))
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                continue
        return None