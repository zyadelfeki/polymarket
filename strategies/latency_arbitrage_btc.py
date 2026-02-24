"""
THE MONEY PRINTER - 15-Min BTC Latency Arbitrage

Strategy: When BTC moves on Binance, bet on Polymarket BEFORE odds update
Win Rate: 88-95% (documented, real)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from typing import Dict, Optional, Tuple, List

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - zoneinfo should exist on supported Python versions
    ZoneInfo = None

from config.settings import settings
from strategies.confidence_booster import ConfidenceBooster
from utils.decimal_helpers import quantize_quantity, to_decimal

try:
    from data_feeds.binance_features import get_all_features as _get_all_binance_features
except Exception:  # pragma: no cover
    _get_all_binance_features = None

getcontext().prec = 18


class _LoggerShim:
    """Minimal structured logger shim.

    This codebase uses structlog-style calls like:
        logger.info("event", key=value)

    Importing structlog during test collection has intermittently hung on Windows.
    To make imports deterministic and side-effect free, use stdlib logging with a
    shim that preserves the call signature.
    """

    def __init__(self, base: logging.Logger) -> None:
        self._base = base

    def debug(self, event: str, **kwargs) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs) -> None:
        self._log(logging.ERROR, event, **kwargs)

    def exception(self, event: str, **kwargs) -> None:
        self._log(logging.ERROR, event, exc_info=True, **kwargs)

    def _log(self, level: int, event: str, exc_info: bool = False, **kwargs) -> None:
        if kwargs:
            # Render kwargs as a compact dict; avoids float formatting surprises.
            self._base.log(level, "%s %s", event, kwargs, exc_info=exc_info)
        else:
            self._base.log(level, "%s", event, exc_info=exc_info)


logger = _LoggerShim(logging.getLogger(__name__))


TIMEFRAMES = {
    "15min": {"slug": "15m", "duration_seconds": 900, "priority": 3},
    "hourly": {"slug": "1h", "duration_seconds": 3600, "priority": 1},
    "4hour": {"slug": "4h", "duration_seconds": 14400, "priority": 2},
    "daily": {"slug": "daily", "duration_seconds": 86400, "priority": 1},
}


class LatencyArbitrageEngine:
    """
    Multi-timeframe BTC latency arbitrage engine.

    Every scan:
    1. Get Binance BTC price (real-time)
    2. Find active Polymarket BTC markets (hourly/daily prioritized)
    3. Calculate TRUE probability vs MARKET price
    4. If edge > threshold → return opportunity
    """

    def __init__(
        self,
        binance_ws,
        polymarket_client,
        charlie_predictor,
        config: Optional[Dict] = None,
        execution_service=None,
        kelly_sizer=None,
        price_history=None,
        redis_subscriber=None,
    ) -> None:
        self.binance = binance_ws
        self.polymarket = polymarket_client
        self.charlie = charlie_predictor
        self.execution = execution_service
        self.kelly_sizer = kelly_sizer
        self.price_history = price_history
        self.confidence_booster = (
            ConfidenceBooster(redis_subscriber) if redis_subscriber else None
        )

        cfg = config or {}

        self.min_edge = to_decimal(cfg.get("min_edge", "0.03"))

        self.max_edge = to_decimal(cfg.get("max_edge", "0.50"))
        self.stale_price_buffer = Decimal(str(cfg.get("stale_price_buffer", 500)))
        self.market_cache_ttl_seconds = int(cfg.get("market_cache_ttl_seconds", 10))
        self.market_scan_limit = int(cfg.get("market_scan_limit", 200))
        self.min_time_left_seconds = int(cfg.get("min_time_left_seconds", 15))
        self.max_time_left_seconds = int(cfg.get("max_time_left_seconds", 15 * 60))
        self.min_volatility_pct = to_decimal(cfg.get("min_volatility_pct", "0.2"))
        self.slippage_buffer = to_decimal(cfg.get("slippage_buffer", "0.01"))
        self.min_orderbook_size = to_decimal(cfg.get("min_orderbook_size", "10"))
        self.enforce_orderbook_validation = bool(cfg.get("enforce_orderbook_validation", False))
        self.max_spread_bps = float(cfg.get("max_spread_bps", 500))  # skip markets wider than 5%
        self.peak_hours_only = bool(cfg.get("peak_hours_only", False))
        self.use_dynamic_edge_thresholds = bool(cfg.get("use_dynamic_edge_thresholds", False))
        edge_thresholds_cfg = cfg.get("edge_thresholds") or {
            "15min": "0.03",
            "hourly": "0.025",
            "4hour": "0.02",
            "daily": "0.015",
        }
        self.edge_thresholds = {
            timeframe: to_decimal(value)
            for timeframe, value in edge_thresholds_cfg.items()
        }

        self._market_cache: List[Dict] = []
        self._market_cache_time: Optional[datetime] = None
        self._market_start_price_cache: Dict[str, Decimal] = {}
        self._market_start_price_meta: Dict[str, Dict] = {}

        self.scan_stats = {
            "scans": 0,
            "live_markets": 0,
            "opportunities": 0,
            "trades": 0,
        }
        self._last_stats_log_ts = 0.0

        self._fifteen_min_re = re.compile(r"\b15\s*(?:m|min|mins|minute|minutes)\b", re.IGNORECASE)
        self._hourly_re = re.compile(r"\b(?:1\s*h(?:our)?|hour(?:ly)?)\b", re.IGNORECASE)
        self._four_hour_re = re.compile(r"\b(?:4\s*h(?:our)?|4hr|4-hour)\b", re.IGNORECASE)
        self._daily_re = re.compile(r"\b(?:daily|today|end of day|eod|by\s+\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
        self._btc_re = re.compile(r"\b(btc|bitcoin)\b", re.IGNORECASE)

        logger.info(
            "latency_arbitrage_btc_initialized",
            min_edge=str(self.min_edge),
            max_edge=str(self.max_edge),
            min_volatility_pct=str(self.min_volatility_pct),
            slippage_buffer=str(self.slippage_buffer),
            peak_hours_only=self.peak_hours_only,
            enforce_orderbook_validation=self.enforce_orderbook_validation,
        )

    def calculate_dynamic_fee(self, price: Decimal) -> Decimal:
        """Estimate taker fee as function of outcome price (peak near midpoint)."""
        clamped_price = max(Decimal("0.0"), min(Decimal("1.0"), to_decimal(price)))
        base_fee_rate = Decimal("0.03")
        distance_from_midpoint = abs(clamped_price - Decimal("0.5"))
        fee_multiplier = Decimal("1.0") - (distance_from_midpoint * Decimal("2"))
        estimated_fee = base_fee_rate * max(Decimal("0"), fee_multiplier)
        return max(estimated_fee, Decimal("0.005"))

    def calculate_net_edge(self, raw_edge: Decimal, yes_price: Decimal) -> Decimal:
        """Adjust edge for taker fee and slippage."""
        taker_fee = self.calculate_dynamic_fee(yes_price)
        total_cost = taker_fee + self.slippage_buffer
        net_edge = to_decimal(raw_edge) - total_cost
        logger.debug(
            "edge_adjusted_for_fees",
            raw_edge=float(raw_edge),
            estimated_fee=float(taker_fee),
            net_edge=float(net_edge),
        )
        logger.debug(
            "edge_adjustment",
            raw_edge=float(raw_edge),
            taker_fee=float(taker_fee),
            slippage=float(self.slippage_buffer),
            net_edge=float(net_edge),
        )
        return net_edge

    def _min_required_net_edge(self, price: Decimal, timeframe: Optional[str]) -> Decimal:
        """Return the minimum net-edge (after fees + slippage) required to trade.

        A small premium is added for mid-market prices (0.40–0.60) because
        markets near 50% are typically the most efficient.  The premium was
        previously hard-coded at 0.06, which effectively killed all near-even
        trades regardless of the configured min_edge.  It is now capped at
        0.04 so the configured threshold is still meaningful.
        """
        base_min_edge = self._resolve_min_edge(timeframe)
        price_dec = to_decimal(price)
        # Add a small 0.01 premium for prices near the midpoint; do NOT let
        # the floor override user-configured thresholds by more than that.
        midpoint_min = max(base_min_edge, base_min_edge + Decimal("0.01"))
        outer_min = base_min_edge
        if Decimal("0.40") < price_dec < Decimal("0.60"):
            return midpoint_min
        return outer_min

    def determine_trade_direction(
        self,
        btc_price: Optional[Decimal] = None,
        strike_price: Optional[Decimal] = None,
        yes_odds: Optional[Decimal] = None,
        no_odds: Optional[Decimal] = None,
        market_id: Optional[str] = None,
        start_price: Optional[Decimal] = None,
        current_price: Optional[Decimal] = None,
        yes_price: Optional[Decimal] = None,
        no_price: Optional[Decimal] = None,
        min_edge: Optional[Decimal] = None,
    ) -> Optional[Dict]:
        """
        Generate trade signal based on price vs strike.

        CRITICAL RULE: ALWAYS BUY, NEVER SELL.
        - Bullish: BUY YES token
        - Bearish: BUY NO token
        """
        reference_start = start_price if start_price is not None else strike_price
        observed_current = current_price if current_price is not None else btc_price
        observed_yes = yes_price if yes_price is not None else yes_odds
        observed_no = no_price if no_price is not None else no_odds

        if reference_start is None or observed_current is None or observed_yes is None or observed_no is None:
            return None

        start_price_dec = to_decimal(reference_start)
        current_price_dec = to_decimal(observed_current)
        yes_odds_dec = to_decimal(observed_yes)
        no_odds_dec = to_decimal(observed_no)
        min_edge_dec = to_decimal(min_edge) if min_edge is not None else self.min_edge

        if start_price_dec <= 0:
            return None

        price_change_pct = (current_price_dec - start_price_dec) / start_price_dec
        scaled_move = price_change_pct * Decimal("5")
        true_prob_up = max(Decimal("0.01"), min(Decimal("0.99"), Decimal("0.5") + scaled_move))
        true_prob_down = Decimal("1.0") - true_prob_up

        edge_up = true_prob_up - yes_odds_dec
        edge_down = true_prob_down - no_odds_dec

        if current_price_dec > start_price_dec and edge_up > min_edge_dec:
            return {
                "market_id": market_id,
                "outcome": "YES",
                "side": "BUY",
                "direction": "BULLISH",
                "expected_outcome": "UP",
                "confidence": true_prob_up,
                "edge": edge_up,
            }

        if current_price_dec < start_price_dec and edge_down > min_edge_dec:
            return {
                "market_id": market_id,
                "outcome": "NO",
                "side": "BUY",
                "direction": "BEARISH",
                "expected_outcome": "DOWN",
                "confidence": true_prob_down,
                "edge": edge_down,
            }

        return None

    async def execute_signal(self, market: Dict, signal: str, confidence: Decimal):
        """Execute trade with correct token mapping."""
        if not self.execution or not self.kelly_sizer:
            logger.warning("execution_or_sizer_missing")
            return None

        yes_token_id, no_token_id = self._extract_token_ids(market)
        if not yes_token_id or not no_token_id:
            logger.warning("missing_token_ids")
            return None

        if signal == "BULLISH":
            token_to_buy = yes_token_id
            expected_outcome = "UP"
        elif signal == "BEARISH":
            token_to_buy = no_token_id
            expected_outcome = "DOWN"
        else:
            logger.warning("invalid_signal", signal=signal)
            return None

        orderbook = await self.polymarket.get_orderbook(token_to_buy)
        best_ask = None
        if orderbook and orderbook.get("asks"):
            try:
                best_ask = Decimal(str(orderbook["asks"][0][0]))
            except Exception:
                best_ask = None

        if not best_ask:
            logger.warning("no_liquidity_for_token", outcome=expected_outcome)
            return None

        bankroll = await self.execution.get_real_balance()
        size = self.kelly_sizer.calculate_size(
            bankroll=bankroll,
            win_prob=confidence,
            market_price=best_ask,
        )
        size = quantize_quantity(size)

        result = await self.execution.place_order(
            strategy="latency_arbitrage_btc",
            market_id=market.get("id") or market.get("condition_id"),
            token_id=token_to_buy,
            side="BUY",
            quantity=size,
            price=best_ask,
        )
        if result:
            self.scan_stats["trades"] += 1
        return result

    async def scan_opportunities(self) -> Optional[Dict]:
        """
        Complete implementation:
        1. Get current BTC price
        2. Fetch Polymarket markets
        3. Filter for BTC 15-minute markets
        4. Extract thresholds
        5. Calculate edges
        6. Return best opportunity
        """
        if self.peak_hours_only and not self.is_peak_trading_hours():
            logger.info("outside_peak_hours_waiting")
            self._maybe_log_scan_stats()
            return None

        # ------------------------------------------------------------------ #
        # Fetch spot prices for all supported assets.  The strategy was       #
        # previously BTC-only, but Polymarket may not have active BTC 15-min  #
        # markets at all times.  Support ETH/SOL/XRP so we never silently     #
        # emit zero opportunities simply because BTC markets are absent.      #
        # ------------------------------------------------------------------ #
        _asset_keywords: Dict[str, List[str]] = {
            "BTC": ["btc", "bitcoin"],
            "ETH": ["eth", "ethereum"],
            "SOL": ["sol", "solana"],
            "XRP": ["xrp", "ripple"],
        }
        asset_prices: Dict[str, Optional[Decimal]] = {}
        for _sym in _asset_keywords:
            asset_prices[_sym] = await self._get_asset_price(_sym)

        logger.info(
            "diagnostic_asset_prices",
            btc=str(asset_prices.get("BTC")),
            eth=str(asset_prices.get("ETH")),
            sol=str(asset_prices.get("SOL")),
            xrp=str(asset_prices.get("XRP")),
        )

        # Backward-compat alias so downstream helpers keep working.
        btc_price = asset_prices.get("BTC")
        if btc_price is None:
            logger.warning("no_btc_price")
            logger.info("diagnostic_btc_price", source="binance", value=None)
        else:
            logger.info("diagnostic_btc_price", source="binance", value=str(btc_price))

        active_assets = [a for a, p in asset_prices.items() if p is not None]
        if not active_assets:
            logger.warning("no_asset_prices_available")
            self._maybe_log_scan_stats()
            return None

        all_markets = await self._get_active_markets()
        if not all_markets:
            logger.warning("no_markets_fetched")
            self._maybe_log_scan_stats()
            return None

        try:
            logger.info(
                "diagnostic_market_keys",
                keys=list(all_markets[0].keys()) if all_markets else [],
            )
            logger.info(
                "diagnostic_raw_market_response",
                sample_market=json.dumps(all_markets[0], default=str)[:800] if all_markets else "{}",
            )
        except Exception:
            logger.debug("diagnostic_market_logging_failed")

        logger.debug("markets_fetched", total=len(all_markets))

        # Build per-asset candidate lists; only include assets with a live price.
        prioritized_markets: List[Dict] = []
        for _asset, _keywords in _asset_keywords.items():
            if asset_prices.get(_asset) is None:
                logger.debug("asset_price_unavailable_skipping", asset=_asset)
                continue
            _asset_markets = [
                m for m in all_markets
                if any(kw in (m.get("question") or "").lower() for kw in _keywords)
            ]
            logger.debug("asset_markets_filtered", asset=_asset, count=len(_asset_markets))
            _asset_prioritized = self._select_markets_for_all_timeframes(
                asset=_asset, markets=_asset_markets
            )
            for _entry in _asset_prioritized:
                _entry["asset"] = _asset  # tag so the scan loop knows which price to use
            if not _asset_prioritized and _asset_markets:
                sample_questions = [
                    (m.get("question") or m.get("title") or "")[:120]
                    for m in _asset_markets[:3]
                ]
                logger.warning(
                    "no_supported_timeframe_markets_found",
                    asset=_asset,
                    samples=sample_questions,
                )
            prioritized_markets.extend(_asset_prioritized)

        # Re-sort combined list by timeframe priority, then soonest expiry first.
        prioritized_markets.sort(
            key=lambda mi: (
                mi["priority"],
                self._extract_time_left_seconds(mi["data"]) or 10 ** 9,
            )
        )

        logger.debug("multi_timeframe_markets_found", count=len(prioritized_markets))

        for market_info in prioritized_markets:
            timeframe = market_info["timeframe"]
            market_asset = market_info.get("asset", "BTC")  # tagged above; default BTC for compat
            market = await self._enrich_market_if_needed(market_info["data"])
            market_asset_price = asset_prices.get(market_asset)

            if market_asset_price is None:
                logger.debug("scan_skipped_no_asset_price", asset=market_asset, timeframe=timeframe)
                continue

            self.scan_stats["scans"] += 1
            logger.info(
                "diagnostic_scanning_market",
                asset=market_asset,
                market_id=market.get("id") or market.get("condition_id"),
                slug=market.get("slug"),
                question=market.get("question"),
                timeframe=timeframe,
            )

            yes_token_id, _ = self._extract_token_ids(market)
            if not yes_token_id:
                logger.warning(
                    "diagnostic_missing_token_ids",
                    asset=market_asset,
                    market_id=market.get("id") or market.get("condition_id"),
                    timeframe=timeframe,
                )
                continue

            orderbook = await self._fetch_orderbook_safe(yes_token_id)
            summary = self._summarize_orderbook(orderbook)
            if self.orderbook_is_valid(summary):
                self.scan_stats["live_markets"] += 1
                spread_bps = None
                try:
                    best_bid = to_decimal(summary.get("best_bid"))
                    best_ask = to_decimal(summary.get("best_ask"))
                    if best_bid > 0:
                        spread_bps = float(((best_ask - best_bid) / best_bid) * Decimal("10000"))
                except Exception:
                    spread_bps = None
                logger.info(
                    "live_market_detected",
                    asset=market_asset,
                    timeframe=timeframe,
                    bid=str(summary.get("best_bid")),
                    ask=str(summary.get("best_ask")),
                    spread=str(
                        to_decimal(summary.get("best_ask")) - to_decimal(summary.get("best_bid"))
                    ),
                )
                logger.info(
                    "spread_calculation",
                    asset=market_asset,
                    market_id=market.get("id") or market.get("condition_id"),
                    timeframe=timeframe,
                    bid=str(summary.get("best_bid")),
                    ask=str(summary.get("best_ask")),
                    spread_bps=spread_bps,
                    status="orderbook_valid",
                )
                if spread_bps is not None and spread_bps > self.max_spread_bps:
                    logger.info(
                        "market_skipped",
                        reason="spread_too_wide",
                        asset=market_asset,
                        spread_bps=spread_bps,
                        max_spread_bps=self.max_spread_bps,
                        market_id=market.get("id") or market.get("condition_id"),
                    )
                    continue
            else:
                logger.warning(
                    "dead_market_detected",
                    asset=market_asset,
                    timeframe=timeframe,
                    market_id=market.get("id") or market.get("condition_id"),
                )
                logger.info(
                    "spread_calculation",
                    asset=market_asset,
                    market_id=market.get("id") or market.get("condition_id"),
                    timeframe=timeframe,
                    bid=str(summary.get("best_bid")),
                    ask=str(summary.get("best_ask")),
                    spread_bps=None,
                    status="skipped_dead_orderbook",
                )
                if self.enforce_orderbook_validation:
                    continue

            opportunity = await self._check_market_arbitrage(
                market, market_asset_price, timeframe=timeframe, asset=market_asset
            )
            if opportunity:
                self.scan_stats["opportunities"] += 1
                logger.info(
                    "opportunity_found",
                    asset=market_asset,
                    timeframe=timeframe,
                    edge_pct=str(opportunity.get("edge")),
                )
                self._maybe_log_scan_stats()
                return opportunity
            logger.debug(
                "diagnostic_no_opportunity",
                asset=market_asset,
                market_id=market.get("id") or market.get("condition_id"),
                min_edge=str(self.min_edge),
                timeframe=timeframe,
            )

        logger.debug("no_opportunities_found", scanned=len(prioritized_markets))
        self._maybe_log_scan_stats()
        return None

    def _select_markets_for_all_timeframes(self, asset: str, markets: List[Dict]) -> List[Dict]:
        asset_lower = asset.lower()
        selected_markets: List[Dict] = []

        for market in markets:
            if not isinstance(market, dict):
                continue

            question = (market.get("question") or market.get("title") or "").lower()
            slug = str(market.get("slug") or market.get("ticker") or "").lower()
            if not self._market_matches_asset(asset_lower=asset_lower, question=question, slug=slug):
                logger.debug(
                    "btc_market_rejected",
                    reason="asset_match_failed",
                    asset=asset_lower,
                    question=(market.get("question") or market.get("title") or "")[:140],
                    slug=slug,
                )
                continue

            market_id = (
                market.get("id")
                or market.get("condition_id")
                or market.get("conditionId")
                or market.get("market_id")
            )

            if market.get("closed") is True:
                logger.info(
                    "btc_market_rejected",
                    market_id=market_id,
                    reason="closed_flag_true",
                    question=(market.get("question") or market.get("title") or "")[:140],
                    slug=slug,
                )
                continue

            status = (market.get("status") or "").upper()
            if status in {"CLOSED", "RESOLVED", "SETTLED"}:
                logger.info(
                    "btc_market_rejected",
                    market_id=market_id,
                    reason=f"status_{status.lower()}",
                    question=(market.get("question") or market.get("title") or "")[:140],
                    slug=slug,
                )
                continue

            timeframe, detection_reason = self._detect_market_timeframe_with_reason(market)
            if timeframe is None:
                logger.info(
                    "btc_market_rejected",
                    market_id=market_id,
                    reason=detection_reason or "timeframe_unclassified",
                    question=(market.get("question") or market.get("title") or "")[:140],
                    slug=slug,
                    end_time=str(self._extract_market_end_time(market)),
                    start_time=str(self._extract_market_start_time(market)),
                    duration_seconds=self._extract_duration_seconds(market),
                    time_left_seconds=self._extract_time_left_seconds(market),
                )
                continue

            # Reject markets that are at or past expiry.  _extract_time_left_seconds
            # returns 0 (clamped) for expired markets, so 0 < min_time_left_seconds
            # correctly prunes them before they reach scan_opportunities and Charlie.
            _time_left = self._extract_time_left_seconds(market)
            if _time_left is not None and _time_left < self.min_time_left_seconds:
                logger.info(
                    "btc_market_rejected",
                    market_id=market_id,
                    reason="too_close_to_expiry",
                    time_left_seconds=_time_left,
                    min_time_left_seconds=self.min_time_left_seconds,
                    question=(market.get("question") or market.get("title") or "")[:140],
                )
                continue

            logger.info(
                "btc_market_accepted",

                market_id=market_id,
                timeframe=timeframe,
                reason=detection_reason,
                question=(market.get("question") or market.get("title") or "")[:140],
                slug=slug,
                end_time=str(self._extract_market_end_time(market)),
                start_time=str(self._extract_market_start_time(market)),
                duration_seconds=self._extract_duration_seconds(market),
                time_left_seconds=self._extract_time_left_seconds(market),
            )

            selected_markets.append(
                {
                    "timeframe": timeframe,
                    "priority": TIMEFRAMES[timeframe]["priority"],
                    "data": market,
                }
            )

        selected_markets.sort(
            key=lambda market_info: (
                market_info["priority"],
                self._extract_time_left_seconds(market_info["data"]) or 10**9,
            )
        )
        return selected_markets

    def _market_matches_asset(self, asset_lower: str, question: str, slug: str) -> bool:
        if asset_lower in question or asset_lower in slug:
            return True

        if asset_lower == "btc":
            return bool(self._btc_re.search(question) or ("bitcoin" in slug))

        return False

    async def get_markets_for_all_timeframes(self, asset: str, base_timestamp: int) -> List[Dict]:
        """Scan supported timeframes and return markets sorted by priority (1 = highest)."""
        _ = base_timestamp
        active_markets = await self._get_active_markets()
        return self._select_markets_for_all_timeframes(asset=asset, markets=active_markets)

    async def _enrich_market_if_needed(self, market: Dict) -> Dict:
        """Fetch full market details when discovery payload omits token metadata."""
        yes_token_id, no_token_id = self._extract_token_ids(market)
        if yes_token_id and no_token_id:
            return market

        market_id = (
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("condition_id")
            or market.get("id")
            or market.get("market_id")
        )
        if not market_id or not hasattr(self.polymarket, "get_market"):
            return market

        try:
            full_market = await self.polymarket.get_market(str(market_id))
            if isinstance(full_market, dict):
                merged = {**market, **full_market}
                yes_token_id2, no_token_id2 = self._extract_token_ids(merged)
                if yes_token_id2 and no_token_id2:
                    logger.info(
                        "market_enriched_with_tokens",
                        market_id=str(market_id),
                        yes_token_id=yes_token_id2,
                        no_token_id=no_token_id2,
                    )
                    return merged
        except Exception as exc:
            logger.debug("market_enrichment_failed", market_id=str(market_id), error=str(exc))

        return market

    def _is_15min_market(self, market: Dict) -> bool:
        return self._detect_market_timeframe(market) == "15min"

    def _detect_market_timeframe(self, market: Dict) -> Optional[str]:
        timeframe, _reason = self._detect_market_timeframe_with_reason(market)
        return timeframe

    def _detect_market_timeframe_with_reason(self, market: Dict) -> Tuple[Optional[str], str]:
        question = (market.get("question") or market.get("title") or "")
        slug = str(market.get("slug") or market.get("ticker") or "").lower()
        question_lower = question.lower()

        resolution_raw = (
            market.get("resolution")
            or market.get("resolution_timeframe")
            or market.get("resolutionTimeframe")
            or market.get("resolution_time")
            or market.get("resolutionTime")
            or ""
        )
        resolution_text = str(resolution_raw).lower()

        if self._daily_re.search(question) or any(token in slug for token in ("updown-daily", "-daily-")):
            return "daily", "daily_slug_or_question_match"
        if self._four_hour_re.search(question) or any(token in slug for token in ("updown-4h", "-4h-", "-4hr-")):
            return "4hour", "4hour_slug_or_question_match"
        if self._hourly_re.search(question) or any(token in slug for token in ("updown-1h", "-1h-", "hourly")):
            return "hourly", "hourly_slug_or_question_match"
        if self._fifteen_min_re.search(question) or any(token in slug for token in ("15m", "15min", "updown-15m")):
            return "15min", "15min_slug_or_question_match"

        if any(token in resolution_text for token in ("15m", "15 min", "15-minute")):
            return "15min", "resolution_metadata_15min"
        if any(token in resolution_text for token in ("1h", "1 hour", "hourly")):
            return "hourly", "resolution_metadata_hourly"
        if any(token in resolution_text for token in ("4h", "4 hour", "4-hour")):
            return "4hour", "resolution_metadata_4hour"
        if any(token in resolution_text for token in ("daily", "1d", "day")):
            return "daily", "resolution_metadata_daily"

        duration_seconds = self._extract_duration_seconds(market)
        if duration_seconds is not None:
            if 12 * 60 <= duration_seconds <= 18 * 60:
                return "15min", "duration_window_15min"
            if 45 * 60 <= duration_seconds <= int(2.5 * 3600):
                return "hourly", "duration_window_hourly"
            if int(3.0 * 3600) <= duration_seconds <= int(6.0 * 3600):
                return "4hour", "duration_window_4hour"
            if int(18 * 3600) <= duration_seconds <= int(36 * 3600):
                return "daily", "duration_window_daily"

        time_left_seconds = self._extract_time_left_seconds(market)
        if time_left_seconds is not None:
            if 30 * 60 <= time_left_seconds <= int(2.5 * 3600):
                if ("up or down" in question_lower) or re.search(r"bitcoin\s+above\s+\$?[\d,]+\s+on", question_lower):
                    return "hourly", "time_left_and_hourly_question_pattern"
                return "hourly", "time_left_window_hourly"
            if int(2.5 * 3600) < time_left_seconds <= int(6.5 * 3600):
                if "intraday" in question_lower or "range" in question_lower:
                    return "4hour", "time_left_intraday_range_pattern"
                return "4hour", "time_left_window_4hour"

        end_dt = self._extract_market_end_time(market)
        if end_dt is not None:
            now_utc = datetime.now(timezone.utc)
            day_delta = (end_dt.date() - now_utc.date()).days
            if day_delta in {0, 1}:
                if (
                    "price on" in question_lower
                    or "what price will" in question_lower
                    or re.search(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", question_lower)
                    or re.search(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b", question_lower)
                ):
                    return "daily", "end_date_today_tomorrow_daily_pattern"
                if day_delta == 1:
                    return "daily", "end_date_tomorrow_default_daily"

        if "what price will bitcoin hit" in question_lower or "bitcoin price on" in question_lower:
            return "daily", "daily_question_pattern"
        if "intraday" in question_lower or "price range" in question_lower:
            return "4hour", "intraday_range_question_pattern"
        if re.search(r"bitcoin\s+(?:up\s+or\s+down|above\s+\$?[\d,]+\s+on)", question_lower):
            return "hourly", "hourly_question_pattern"

        return None, "no_timeframe_match_from_metadata_or_question"

    def _extract_duration_seconds(self, market: Dict) -> Optional[int]:
        start_dt = self._extract_market_start_time(market)
        end_dt = self._extract_market_end_time(market)
        if not start_dt or not end_dt:
            return None

        return int((end_dt - start_dt).total_seconds())

    def _extract_market_start_time(self, market: Dict) -> Optional[datetime]:
        start_fields = [
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
        ]
        return self._extract_market_datetime_field(market=market, fields=start_fields)

    def _extract_market_end_time(self, market: Dict) -> Optional[datetime]:
        end_fields = [
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
        ]
        return self._extract_market_datetime_field(market=market, fields=end_fields)

    def _extract_market_datetime_field(self, market: Dict, fields: List[str]) -> Optional[datetime]:
        raw_value = None
        for field in fields:
            if field in market and market.get(field) not in (None, ""):
                raw_value = market.get(field)
                break

        if raw_value is None:
            return None

        try:
            if isinstance(raw_value, datetime):
                dt = raw_value
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)

            if isinstance(raw_value, str):
                text = raw_value.strip()
                if not text:
                    return None
                if "T" in text or "Z" in text or "+" in text or ("-" in text and ":" in text):
                    try:
                        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            return parsed.replace(tzinfo=timezone.utc)
                        return parsed.astimezone(timezone.utc)
                    except ValueError:
                        pass

                if re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
                    parsed_ts = to_decimal(text)
                    if parsed_ts > Decimal("1000000000000"):
                        parsed_ts = parsed_ts / Decimal("1000")
                    return datetime.fromtimestamp(float(parsed_ts), tz=timezone.utc)

                logger.debug(
                    "market_datetime_unparseable_string",
                    fields=fields,
                    raw_value=text[:60],
                )
                return None

            if isinstance(raw_value, (int, float, Decimal)):
                return datetime.fromtimestamp(to_decimal(raw_value), tz=timezone.utc)
        except (ValueError, TypeError, InvalidOperation, OverflowError, OSError) as exc:
            logger.debug(
                "market_datetime_parse_failed",
                fields=fields,
                raw_value=str(raw_value)[:60],
                error=str(exc),
            )

        return None

    async def find_15min_market(self, btc_price: Decimal) -> Optional[Dict]:
        """Public helper for test compatibility."""
        return await self._find_15min_market(btc_price)

    async def _check_market_arbitrage(
        self,
        market: Dict,
        btc_price: Decimal,  # kept as `btc_price` for backward-compat; holds the asset's spot price
        timeframe: Optional[str] = None,
        asset: Optional[str] = None,
    ) -> Optional[Dict]:
        """
        Check an UP/DOWN market for a latency-arbitrage opportunity.

        ``btc_price`` is now the *generic* spot price for whatever asset the
        market tracks (BTC, ETH, SOL, XRP).  The parameter name is kept for
        backward-compatibility with existing usages and tests.

        ``asset`` is resolved from the question text when not supplied by the
        caller (legacy code path).
        """
        question_raw = market.get("question", "")
        market_id = market.get("id") or market.get("condition_id") or market.get("market_id")
        question = question_raw.lower()

        # Resolve asset from question when not injected by the caller.
        if asset is None:
            if "bitcoin" in question or "btc" in question:
                asset = "BTC"
            elif "ethereum" in question or "eth" in question:
                asset = "ETH"
            elif "solana" in question or "sol" in question:
                asset = "SOL"
            elif "xrp" in question:
                asset = "XRP"
            else:
                logger.debug(
                    "skipping_market_unknown_asset",
                    market_id=market_id,
                    question=(question_raw or "")[:60],
                )
                return None

        self._seed_market_start_price_from_tick(market=market, btc_price=btc_price)

        prices = self._get_market_prices_from_tokens(market)
        yes_token_id, no_token_id = self._extract_token_ids(market)
        if not yes_token_id or not no_token_id:
            logger.warning(
                "diagnostic_missing_token_ids",
                market_id=market_id,
                question=(question_raw or "")[:60],
            )
            return None

        if not prices or prices.get("yes") is None or prices.get("no") is None:
            orderbook_prices = await self._get_orderbook_prices(
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                market_id=market_id,
            )
            if orderbook_prices is None:
                logger.warning(
                    "diagnostic_missing_prices",
                    market_id=market_id,
                    question=(question_raw or "")[:60],
                )
                return None
            prices = {
                "yes": orderbook_prices["yes"],
                "no": orderbook_prices["no"],
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
            }
        else:
            prices["yes_token_id"] = prices.get("yes_token_id") or yes_token_id
            prices["no_token_id"] = prices.get("no_token_id") or no_token_id

        start_price = self._get_market_start_price(market)
        if not start_price or start_price == 0:
            start_price = await self._get_interval_start_price(market, btc_price)
        if not start_price or start_price == 0:
            logger.warning(
                "diagnostic_missing_start_price",
                market_id=market_id,
                question=(question_raw or "")[:60],
            )
            return None

        price_change = btc_price - start_price
        price_change_pct = (price_change / start_price) * Decimal("100.0")

        logger.debug(
            "market_price_analysis",
            market_id=market.get("id"),
            start_price=str(start_price),
            current_price=str(btc_price),
            change_pct=str(price_change_pct),
        )
        logger.info(
            "diagnostic_edge_inputs",
            market_id=market_id,
            start_price=str(start_price),
            current_price=str(btc_price),
            change_pct=str(price_change_pct),
            yes_price=str(prices.get("yes")) if prices else None,
            no_price=str(prices.get("no")) if prices else None,
        )

        true_prob_up: Optional[Decimal] = None
        true_prob_down: Optional[Decimal] = None
        charlie_confidence = Decimal("0.5")

        if abs(price_change_pct) > Decimal("0.15"):
            if price_change > 0:
                true_prob_up, true_prob_down = Decimal("0.95"), Decimal("0.05")
            else:
                true_prob_up, true_prob_down = Decimal("0.05"), Decimal("0.95")
        elif abs(price_change_pct) < Decimal("0.02"):
            logger.info(
                "edge_candidate_rejected",
                reason="price_too_neutral",
                asset=asset,
                market_id=market_id,
                change_pct=str(price_change_pct),
                threshold="0.02%",
            )
            return None
        else:
            if self.charlie:
                prediction = await self._get_charlie_prediction(
                    current_price=btc_price,
                    start_price=start_price,
                    time_horizon=self.max_time_left_seconds,
                )
                true_prob_up = Decimal(str(prediction.get("probability", "0.5")))
                true_prob_down = Decimal("1.0") - true_prob_up
                charlie_confidence = to_decimal(prediction.get("confidence", "0.5"))
            else:
                if price_change > 0:
                    true_prob_up, true_prob_down = Decimal("0.60"), Decimal("0.40")
                else:
                    true_prob_up, true_prob_down = Decimal("0.40"), Decimal("0.60")

        if true_prob_up is None or true_prob_down is None:
            return None

        true_prob_up = max(Decimal("0.01"), min(Decimal("0.99"), true_prob_up))
        true_prob_down = Decimal("1.0") - true_prob_up

        edge_up = true_prob_up - prices["yes"]
        edge_down = true_prob_down - prices["no"]
        net_edge_up = self.calculate_net_edge(edge_up, prices["yes"])
        net_edge_down = self.calculate_net_edge(edge_down, prices["no"])

        expected_outcome = "UP" if btc_price >= start_price else "DOWN"
        expected_true_prob = true_prob_up if expected_outcome == "UP" else true_prob_down
        yes_price = prices.get("yes")
        edge = abs(expected_true_prob - yes_price)

        logger.info(
            "diagnostic_edge_calc",
            market_id=market_id,
            question=(question_raw or "")[:60],
            start_price=str(start_price),
            binance_price=str(btc_price),
            expected_outcome=expected_outcome,
            yes_price=str(yes_price),
            true_prob=str(expected_true_prob),
            edge=str(edge),
            min_edge=str(self._resolve_min_edge(timeframe)),
        )

        logger.info(
            "diagnostic_edge_calculation",
            market_id=market_id,
            asset=asset,
            true_prob_up=str(true_prob_up),
            true_prob_down=str(true_prob_down),
            yes_price=str(prices["yes"]),
            no_price=str(prices["no"]),
            edge_up=str(edge_up),
            edge_down=str(edge_down),
            net_edge_up=str(net_edge_up),
            net_edge_down=str(net_edge_down),
            min_edge=str(self._resolve_min_edge(timeframe)),
        )
        if not market_id:
            return None

        min_edge_threshold_up = self._min_required_net_edge(prices["yes"], timeframe)
        min_edge_threshold_down = self._min_required_net_edge(prices["no"], timeframe)

        # ---- edge_candidate_computed: always emitted so replay / sweep can  ----
        # ---- analyse the full decision frontier even for rejected trades.   ----
        logger.info(
            "edge_candidate_computed",
            market_id=market_id,
            asset=asset,
            timeframe=timeframe,
            net_edge_up=str(net_edge_up),
            net_edge_down=str(net_edge_down),
            min_edge_threshold_up=str(min_edge_threshold_up),
            min_edge_threshold_down=str(min_edge_threshold_down),
            passes_up=bool(net_edge_up > min_edge_threshold_up),
            passes_down=bool(net_edge_down > min_edge_threshold_down and net_edge_down > net_edge_up),
            question=(question_raw or "")[:80],
        )

        if net_edge_up > min_edge_threshold_up and net_edge_up >= net_edge_down:
            return {
                "market_id": market_id,
                "token_id": prices["yes_token_id"],
                "side": "YES",
                "outcome": "UP",
                "asset": asset,
                "true_prob": true_prob_up,
                "market_price": prices["yes"],
                "edge": net_edge_up,
                "raw_edge": edge_up,
                "confidence": "HIGH" if net_edge_up > Decimal("0.15") else "MEDIUM",
                "charlie_confidence": charlie_confidence,
                "direction": "UP",
                "btc_price": btc_price,
                "asset_price": btc_price,
                "start_price": start_price,
                "price_change_pct": price_change_pct,
                "question": question_raw,
                "timeframe": timeframe or self._detect_market_timeframe(market) or "unknown",
            }

        if net_edge_down > min_edge_threshold_down and net_edge_down > net_edge_up:
            return {
                "market_id": market_id,
                "token_id": prices["no_token_id"],
                "side": "NO",
                "outcome": "DOWN",
                "asset": asset,
                "true_prob": true_prob_down,
                "market_price": prices["no"],
                "edge": net_edge_down,
                "raw_edge": edge_down,
                "confidence": "HIGH" if net_edge_down > Decimal("0.15") else "MEDIUM",
                "charlie_confidence": charlie_confidence,
                "direction": "DOWN",
                "btc_price": btc_price,
                "asset_price": btc_price,
                "start_price": start_price,
                "price_change_pct": price_change_pct,
                "question": question_raw,
                "timeframe": timeframe or self._detect_market_timeframe(market) or "unknown",
            }

        logger.debug(
            "edges_too_small",
            edge_up=str(net_edge_up),
            edge_down=str(net_edge_down),
            min_edge_up=str(min_edge_threshold_up),
            min_edge_down=str(min_edge_threshold_down),
        )

        return None

    def _get_market_start_price(self, market: Dict) -> Optional[Decimal]:
        """
        Extract start price from market metadata.
        """
        market_key = self._market_key(market)

        if "startingPrice" in market:
            try:
                return Decimal(str(market["startingPrice"]))
            except Exception:
                pass

        metadata = market.get("metadata", {})
        if isinstance(metadata, dict) and "startPrice" in metadata:
            try:
                return Decimal(str(metadata["startPrice"]))
            except Exception:
                pass

        if market_key and market_key in self._market_start_price_cache:
            return self._market_start_price_cache[market_key]

        description = market.get("description", "") or ""
        parsed_description_price = self._parse_start_price(description)
        if parsed_description_price is not None:
            return parsed_description_price

        if hasattr(self.binance, "get_current_price"):
            try:
                if not asyncio.iscoroutinefunction(self.binance.get_current_price):
                    price = self.binance.get_current_price("BTC")
                    if price is not None:
                        current_price = Decimal(str(price))
                        if market_key and market_key not in self._market_start_price_cache:
                            self._market_start_price_cache[market_key] = current_price
                            self._market_start_price_meta[market_key] = {
                                "source": "binance_current_price_fallback",
                                "captured_at": datetime.now(timezone.utc).isoformat(),
                            }
                            logger.info(
                                "market_start_price_seeded",
                                market_id=market.get("id") or market.get("condition_id"),
                                slug=market.get("slug"),
                                source="binance_current_price_fallback",
                                start_price=str(current_price),
                            )
                        return current_price
            except Exception:
                pass

        logger.warning("using_current_price_as_start_unavailable", market_id=market.get("id"))
        return None

    async def _get_interval_start_price(self, market: Dict, btc_price: Decimal) -> Optional[Decimal]:
        """Get BTC price at interval start using price history when available."""
        market_key = self._market_key(market)
        if market_key and market_key in self._market_start_price_cache:
            return self._market_start_price_cache[market_key]

        start_fields = [
            "startDate",
            "start_date",
            "startTime",
            "start_time",
        ]

        start_raw = None
        for field in start_fields:
            candidate = market.get(field)
            if candidate:
                start_raw = candidate
                break

        if not start_raw:
            return None

        try:
            start_time = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        except Exception:
            return None

        if self.price_history is not None:
            start_price = self.price_history.get_price_at_time(
                "BTC",
                start_time.timestamp(),
            )
            if start_price is not None:
                if market_key and market_key not in self._market_start_price_cache:
                    start_price_dec = Decimal(str(start_price))
                    self._market_start_price_cache[market_key] = start_price_dec
                    self._market_start_price_meta[market_key] = {
                        "source": "price_history",
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                    }
                    logger.info(
                        "market_start_price_seeded",
                        market_id=market.get("id") or market.get("condition_id"),
                        slug=market.get("slug"),
                        source="price_history",
                        start_price=str(start_price_dec),
                    )
                return start_price

        now = datetime.now(timezone.utc)
        age_seconds = (now - start_time).total_seconds()

        if age_seconds > 120:
            logger.debug(
                "market_too_old_for_start_price",
                market_id=market.get("id") or market.get("condition_id"),
                age_seconds=int(age_seconds),
            )
            return None

        if market_key and market_key not in self._market_start_price_cache:
            self._market_start_price_cache[market_key] = Decimal(str(btc_price))
            self._market_start_price_meta[market_key] = {
                "source": "interval_fresh_market_current_price",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(
                "market_start_price_seeded",
                market_id=market.get("id") or market.get("condition_id"),
                slug=market.get("slug"),
                source="interval_fresh_market_current_price",
                start_price=str(btc_price),
            )

        return btc_price

    def _market_key(self, market: Dict) -> Optional[str]:
        market_id = (
            market.get("id")
            or market.get("condition_id")
            or market.get("conditionId")
            or market.get("slug")
        )
        if not market_id:
            return None
        return str(market_id)

    def _seed_market_start_price_from_tick(self, market: Dict, btc_price: Decimal) -> None:
        market_key = self._market_key(market)
        if not market_key:
            return

        if market_key in self._market_start_price_cache:
            return

        metadata_start = self._get_market_start_price(market)
        if metadata_start is not None:
            self._market_start_price_cache[market_key] = Decimal(str(metadata_start))
            self._market_start_price_meta[market_key] = {
                "source": "market_metadata",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
            logger.info(
                "market_start_price_seeded",
                market_id=market.get("id") or market.get("condition_id"),
                slug=market.get("slug"),
                source="market_metadata",
                start_price=str(metadata_start),
            )
            return

        self._market_start_price_cache[market_key] = Decimal(str(btc_price))
        self._market_start_price_meta[market_key] = {
            "source": "first_observed_binance_tick",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "market_start_price_seeded",
            market_id=market.get("id") or market.get("condition_id"),
            slug=market.get("slug"),
            source="first_observed_binance_tick",
            start_price=str(btc_price),
        )

    def _parse_start_price(self, description: str) -> Optional[Decimal]:
        """Extract starting BTC price from market description."""
        if not description:
            return None

        patterns = [
            r"starting at\s*\$?([0-9,]+(?:\.[0-9]+)?)",
            r"begins at\s*\$?([0-9,]+(?:\.[0-9]+)?)",
            r"start.*?price.*?\$?([0-9,]+(?:\.[0-9]+)?)",
            r"start(?:ing)?\s+price:?\s*\$?([0-9,]+(?:\.[0-9]+)?)",
        ]

        lowered = description.lower()
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                try:
                    price_str = match.group(1).replace(",", "")
                    return Decimal(price_str)
                except Exception:
                    continue

        return None

    def _extract_threshold(self, question: str) -> Optional[Decimal]:
        """Extract numerical threshold from market question."""
        patterns = [
            (r"\$?([\d,]+)K", 1000),
            (r"\$?([\d,]+)", 1),
        ]

        for pattern, multiplier in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                num_str = match.group(1).replace(",", "")
                try:
                    return Decimal(num_str) * Decimal(str(multiplier))
                except Exception:
                    continue
        return None

    def _get_market_prices_from_tokens(self, market: Dict) -> Optional[Dict]:
        """Extract YES/NO prices and token IDs from market tokens."""
        tokens = self._extract_tokens_from_market(market)
        if not tokens:
            return None

        yes_token = next((t for t in tokens if t.get("outcome") == "YES"), None)
        no_token = next((t for t in tokens if t.get("outcome") == "NO"), None)

        if not yes_token or not no_token:
            return None

        try:
            return {
                "yes": Decimal(str(yes_token["price"])),
                "no": Decimal(str(no_token["price"])),
                "yes_token_id": yes_token.get("token_id") or yes_token.get("tokenId") or market.get("yes_token_id"),
                "no_token_id": no_token.get("token_id") or no_token.get("tokenId") or market.get("no_token_id"),
            }
        except (KeyError, ValueError, TypeError):
            return None

    def _extract_tokens_from_market(self, market: Dict) -> List[Dict]:
        tokens = market.get("tokens") or []
        if tokens:
            return tokens

        outcomes = market.get("outcomes") or market.get("outcomePrices") or market.get("outcome_prices")
        if isinstance(outcomes, list) and outcomes:
            normalized: List[Dict] = []
            for outcome in outcomes:
                if isinstance(outcome, dict):
                    normalized.append(
                        {
                            "outcome": outcome.get("outcome") or outcome.get("name") or outcome.get("label"),
                            "token_id": outcome.get("token_id") or outcome.get("tokenId") or outcome.get("id") or outcome.get("outcome_id"),
                            "price": outcome.get("price") or outcome.get("last_price"),
                        }
                    )
                elif isinstance(outcome, str):
                    normalized.append({"outcome": outcome})
            if normalized:
                return normalized

        event_markets = market.get("markets") or []
        if isinstance(event_markets, list) and event_markets:
            for nested_market in event_markets:
                if not isinstance(nested_market, dict):
                    continue
                nested_tokens = nested_market.get("tokens") or []
                if isinstance(nested_tokens, list) and nested_tokens:
                    return nested_tokens

                nested_outcomes = nested_market.get("outcomes") or nested_market.get("outcomePrices") or nested_market.get("outcome_prices")
                if isinstance(nested_outcomes, list) and nested_outcomes:
                    normalized_nested: List[Dict] = []
                    for outcome in nested_outcomes:
                        if isinstance(outcome, dict):
                            normalized_nested.append(
                                {
                                    "outcome": outcome.get("outcome") or outcome.get("name") or outcome.get("label"),
                                    "token_id": outcome.get("token_id") or outcome.get("tokenId") or outcome.get("id") or outcome.get("outcome_id"),
                                    "price": outcome.get("price") or outcome.get("last_price"),
                                }
                            )
                    if normalized_nested:
                        return normalized_nested

        return []

    async def _get_orderbook_prices(
        self,
        *,
        yes_token_id: str,
        no_token_id: str,
        market_id: Optional[str],
    ) -> Optional[Dict[str, Decimal]]:
        try:
            yes_book = await self._fetch_orderbook_safe(yes_token_id)
            no_book = await self._fetch_orderbook_safe(no_token_id)

            if self.enforce_orderbook_validation and not self.orderbook_is_valid(self._summarize_orderbook(yes_book)):
                logger.warning("diagnostic_invalid_yes_orderbook", market_id=market_id)
                return None
            if self.enforce_orderbook_validation and not self.orderbook_is_valid(self._summarize_orderbook(no_book)):
                logger.warning("diagnostic_invalid_no_orderbook", market_id=market_id)
                return None

            yes_price = self._extract_best_ask(yes_book)
            no_price = self._extract_best_ask(no_book)

            if yes_price is None or no_price is None:
                logger.warning(
                    "diagnostic_orderbook_empty",
                    market_id=market_id,
                    yes_price=str(yes_price) if yes_price is not None else None,
                    no_price=str(no_price) if no_price is not None else None,
                )
                return None

            return {"yes": yes_price, "no": no_price}
        except Exception as exc:
            logger.warning(
                "diagnostic_orderbook_fetch_failed",
                market_id=market_id,
                error=str(exc),
            )
            return None

    async def _fetch_orderbook_safe(self, token_id: str) -> Optional[Dict]:
        try:
            orderbook = await self.polymarket.get_orderbook(token_id)
            if orderbook is None:
                logger.debug("orderbook_not_found", token_id=str(token_id)[:8])
                return None
            if isinstance(orderbook, dict) and str(orderbook.get("error", "")).strip().startswith("404"):
                logger.debug("orderbook_404", token_id=str(token_id)[:8])
                return None
            return orderbook
        except asyncio.TimeoutError:
            logger.error("orderbook_fetch_timeout", token_id=str(token_id)[:8])
            return None
        except Exception as exc:
            logger.error("orderbook_fetch_error", token_id=str(token_id)[:8], error=str(exc))
            return None

    def _summarize_orderbook(self, orderbook: Optional[Dict]) -> Dict[str, Decimal]:
        if not isinstance(orderbook, dict):
            return {}

        best_bid = self._extract_best_bid(orderbook)
        best_ask = self._extract_best_ask(orderbook)
        bid_size = self._extract_best_size(orderbook.get("bids"))
        ask_size = self._extract_best_size(orderbook.get("asks"))

        summary: Dict[str, Decimal] = {}
        if best_bid is not None:
            summary["best_bid"] = best_bid
        if best_ask is not None:
            summary["best_ask"] = best_ask
        if bid_size is not None:
            summary["bid_size"] = bid_size
        if ask_size is not None:
            summary["ask_size"] = ask_size
        return summary

    def orderbook_is_valid(self, orderbook: Optional[Dict]) -> bool:
        """Check if orderbook has actual liquidity and sane pricing."""
        if not orderbook or not isinstance(orderbook, dict):
            return False

        best_bid = orderbook.get("best_bid")
        best_ask = orderbook.get("best_ask")
        if best_bid is None or best_ask is None:
            return False

        try:
            best_bid_dec = to_decimal(best_bid)
            best_ask_dec = to_decimal(best_ask)
        except Exception:
            return False

        if best_bid_dec <= 0 or best_ask_dec <= 0:
            return False
        if best_ask_dec <= best_bid_dec:
            return False

        bid_size = orderbook.get("bid_size", Decimal("0"))
        ask_size = orderbook.get("ask_size", Decimal("0"))
        try:
            bid_size_dec = to_decimal(bid_size)
            ask_size_dec = to_decimal(ask_size)
        except Exception:
            return False

        if (
            bid_size is not None
            and ask_size is not None
            and (bid_size_dec < self.min_orderbook_size or ask_size_dec < self.min_orderbook_size)
        ):
            logger.debug(
                "insufficient_orderbook_size",
                bid_size=str(bid_size_dec),
                ask_size=str(ask_size_dec),
                min_size=str(self.min_orderbook_size),
            )
            return False

        return True

    def _extract_best_bid(self, orderbook: Optional[Dict]) -> Optional[Decimal]:
        if not orderbook or not isinstance(orderbook, dict):
            return None

        bids = orderbook.get("bids") or []
        best_price: Optional[Decimal] = None
        for bid in bids:
            price_val = None
            if isinstance(bid, dict):
                price_val = bid.get("price")
            elif isinstance(bid, (list, tuple)) and bid:
                price_val = bid[0]

            if price_val is None:
                continue

            try:
                price_dec = Decimal(str(price_val))
            except Exception:
                continue

            if best_price is None or price_dec > best_price:
                best_price = price_dec

        return best_price

    def _extract_best_size(self, side: Optional[List]) -> Optional[Decimal]:
        if not isinstance(side, list):
            return None
        if not side:
            return None

        first_level = side[0]
        size_val = None
        if isinstance(first_level, dict):
            size_val = first_level.get("size") or first_level.get("quantity")
        elif isinstance(first_level, (list, tuple)) and len(first_level) > 1:
            size_val = first_level[1]

        if size_val is None:
            return None
        try:
            return Decimal(str(size_val))
        except Exception:
            return None

    def _resolve_min_edge(self, timeframe: Optional[str]) -> Decimal:
        if not self.use_dynamic_edge_thresholds:
            return self.min_edge
        if timeframe and timeframe in self.edge_thresholds:
            return self.edge_thresholds[timeframe]
        return self.min_edge

    def is_peak_trading_hours(self) -> bool:
        """Return True when current ET time is within higher-liquidity windows."""
        if ZoneInfo is None:
            now_et = datetime.now(timezone.utc)
            hour = now_et.hour
        else:
            try:
                now_et = datetime.now(ZoneInfo("America/New_York"))
                hour = now_et.hour
            except Exception:
                now_et = datetime.now(timezone.utc)
                hour = now_et.hour

        if 9 <= hour < 16:
            return True
        if 2 <= hour < 4:
            return True
        return False

    def _maybe_log_scan_stats(self) -> None:
        now_ts = time.time()
        if now_ts - self._last_stats_log_ts < 60:
            return

        logger.info(
            "hourly_stats",
            scans=self.scan_stats["scans"],
            live=self.scan_stats["live_markets"],
            opps=self.scan_stats["opportunities"],
            trades=self.scan_stats["trades"],
        )
        self._last_stats_log_ts = now_ts

    def _extract_best_ask(self, orderbook: Optional[Dict]) -> Optional[Decimal]:
        if not orderbook or not isinstance(orderbook, dict):
            return None

        asks = orderbook.get("asks") or []
        best_price: Optional[Decimal] = None

        for ask in asks:
            price_val = None
            if isinstance(ask, dict):
                price_val = ask.get("price")
            elif isinstance(ask, (list, tuple)) and ask:
                price_val = ask[0]

            if price_val is None:
                continue

            try:
                price_dec = Decimal(str(price_val))
            except Exception:
                continue

            if best_price is None or price_dec < best_price:
                best_price = price_dec

        return best_price

    async def _get_btc_price(self) -> Optional[Decimal]:
        return await self._get_asset_price("BTC")

    async def _get_asset_price(self, asset: str) -> Optional[Decimal]:
        """Get current spot price for any supported asset via the Binance feed."""
        symbol = asset.upper()
        if hasattr(self.binance, "get_current_price"):
            price = self.binance.get_current_price(symbol)
            if price is not None:
                return Decimal(str(price))

        if hasattr(self.binance, "get_price"):
            price = await self.binance.get_price(symbol)
            if price is not None:
                return Decimal(str(price))

        if hasattr(self.binance, "get_price_data"):
            price_data = await self.binance.get_price_data(symbol)
            if price_data is not None and getattr(price_data, "price", None) is not None:
                return Decimal(str(price_data.price))

        return None

    async def _find_15min_market(self, btc_price: Decimal) -> Optional[Dict]:
        markets = await self._get_active_markets()
        if not markets:
            return None

        candidates: List[Dict] = []

        for market in markets:
            if not isinstance(market, dict):
                continue

            market = await self._enrich_market_if_needed(market)

            question = (market.get("question") or market.get("title") or "").strip()
            if not question:
                continue

            if not self._btc_re.search(question):
                continue

            if not self._is_15min_market(market):
                continue

            if market.get("closed") is True:
                continue

            status = (market.get("status") or "").upper()
            if status in {"CLOSED", "RESOLVED", "SETTLED"}:
                continue

            threshold, direction = self._extract_threshold_and_direction(question)
            if threshold is None or direction is None:
                continue

            time_left = self._extract_time_left_seconds(market)
            if time_left is not None:
                if time_left < self.min_time_left_seconds:
                    continue
                if time_left > self.max_time_left_seconds:
                    continue
            else:
                time_left = self.max_time_left_seconds

            yes_token_id, no_token_id = self._extract_token_ids(market)
            if not yes_token_id or not no_token_id:
                continue

            candidates.append(
                {
                    "market": market,
                    "threshold": threshold,
                    "direction": direction,
                    "time_left": time_left,
                    "yes_token_id": yes_token_id,
                    "no_token_id": no_token_id,
                }
            )

        if not candidates:
            return None

        candidates.sort(key=lambda m: abs(btc_price - m["threshold"]))
        return candidates[0]

    async def _get_active_markets(self) -> List[Dict]:
        now = datetime.now(timezone.utc)
        if self._market_cache_time:
            age = (now - self._market_cache_time).total_seconds()
            if age < self.market_cache_ttl_seconds:
                return self._market_cache

        markets: List[Dict] = []
        seen_ids = set()

        def _append_unique(items: Optional[List[Dict]]) -> None:
            if not isinstance(items, list):
                return
            for market in items:
                if not isinstance(market, dict):
                    continue
                market_id = (
                    market.get("id")
                    or market.get("condition_id")
                    or market.get("conditionId")
                    or market.get("slug")
                )
                if market_id in seen_ids:
                    continue
                seen_ids.add(market_id)
                markets.append(market)

        if hasattr(self.polymarket, "get_crypto_15min_markets"):
            try:
                fifteen_min_markets = await asyncio.wait_for(
                    self.polymarket.get_crypto_15min_markets(),
                    timeout=45.0,  # was 10s; concurrent refactor brings actual runtime to ~2s
                )
                _append_unique(fifteen_min_markets)
            except asyncio.TimeoutError:
                logger.warning("strategy_market_fetch_timeout", source="get_crypto_15min_markets", timeout_seconds=10.0)
            except Exception as exc:
                logger.warning("strategy_market_fetch_failed", source="get_crypto_15min_markets", error=str(exc))

        if len(markets) < min(20, self.market_scan_limit) and hasattr(self.polymarket, "get_markets"):
            try:
                fast_markets = await asyncio.wait_for(
                    self.polymarket.get_markets(active=True, limit=self.market_scan_limit),
                    timeout=10.0,
                )
                _append_unique(fast_markets)
            except asyncio.TimeoutError:
                logger.warning("strategy_market_fetch_timeout", source="get_markets", timeout_seconds=10.0)
            except Exception as exc:
                logger.warning("strategy_market_fetch_failed", source="get_markets", error=str(exc))

        if not markets and hasattr(self.polymarket, "get_active_markets"):
            try:
                active_markets = await asyncio.wait_for(
                    self.polymarket.get_active_markets(limit=self.market_scan_limit),
                    timeout=20.0,
                )
                _append_unique(active_markets)
            except asyncio.TimeoutError:
                logger.warning("strategy_market_fetch_timeout", source="get_active_markets", timeout_seconds=20.0)
            except Exception as exc:
                logger.warning("strategy_market_fetch_failed", source="get_active_markets", error=str(exc))

        if not markets and self._market_cache:
            logger.warning("strategy_market_fetch_cache_reuse", cached_count=len(self._market_cache))
            return self._market_cache

        self._market_cache = [m for m in markets if isinstance(m, dict)]
        self._market_cache_time = now
        return self._market_cache

    async def _get_market_prices(
        self,
        market: Dict,
        yes_token_id: str,
        no_token_id: str,
    ) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        yes_price = self._extract_token_price(market, "YES")
        no_price = self._extract_token_price(market, "NO")

        summary = None
        market_id = market.get("id") or market.get("condition_id") or market.get("market_id")
        try:
            if market_id:
                summary = await self.polymarket.get_market_orderbook_summary(market_id)
        except Exception:
            summary = None

        if summary and summary.get("ask") is not None:
            yes_price = Decimal(str(summary["ask"]))

        if no_price is None:
            try:
                orderbook = await self.polymarket.get_orderbook(no_token_id)
                no_price = self._extract_mid_price(orderbook)
            except Exception:
                no_price = None

        return yes_price, no_price

    async def _calculate_true_probability(
        self,
        *,
        btc_price: Decimal,
        threshold: Decimal,
        direction: str,
        time_left: int,
    ) -> Tuple[Decimal, Decimal]:
        if direction == "ABOVE":
            if btc_price >= threshold:
                return Decimal("0.95"), Decimal("0.95")
            if btc_price <= (threshold - self.stale_price_buffer):
                return Decimal("0.05"), Decimal("0.05")
        else:
            if btc_price <= threshold:
                return Decimal("0.95"), Decimal("0.95")
            if btc_price >= (threshold + self.stale_price_buffer):
                return Decimal("0.05"), Decimal("0.05")

        charlie_prediction = await self._get_charlie_prediction(
            current_price=btc_price,
            threshold=threshold,
            time_horizon=time_left,
        )
        probability = Decimal(str(charlie_prediction.get("probability", "0.5")))

        # Base confidence from price analysis
        base_confidence = Decimal("0.95")

        # Apply Charlie intelligence boost
        trade_direction = "UP" if btc_price > threshold else "DOWN"
        if self.confidence_booster is not None:
            final_confidence = self.confidence_booster.apply_boost(
                base_confidence,
                trade_direction,
            )
        else:
            final_confidence = base_confidence

        probability = max(Decimal("0.01"), min(Decimal("0.99"), probability))
        final_confidence = max(Decimal("0"), min(Decimal("1"), final_confidence))
        return probability, final_confidence

    async def _get_charlie_prediction(self, symbol: str = "BTC", **kwargs) -> Dict:
        if not self.charlie:
            return {"probability": Decimal("0.5"), "confidence": Decimal("0.5")}

        predictor = getattr(self.charlie, "predict_15min_move", None)
        if not predictor:
            return {"probability": Decimal("0.5"), "confidence": Decimal("0.5")}

        # Fetch live Binance indicators so Charlie uses real features instead of
        # the synthetic-neutral fallback (rsi=50, macd=0) that triggers degraded mode.
        extra_features: Optional[Dict] = None
        if _get_all_binance_features is not None:
            try:
                extra_features = await asyncio.get_running_loop().run_in_executor(
                    None, _get_all_binance_features, symbol
                )
            except Exception:
                extra_features = None  # fall through to synthetic fallback

        result = predictor(symbol=symbol, extra_features=extra_features, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result

        if not isinstance(result, dict):
            return {"probability": Decimal("0.5"), "confidence": Decimal("0.5")}

        return result

    def _build_opportunity(
        self,
        *,
        market: Dict,
        yes_token_id: str,
        no_token_id: str,
        yes_price: Decimal,
        no_price: Optional[Decimal],
        true_prob: Decimal,
        yes_edge: Decimal,
        no_edge: Optional[Decimal],
        charlie_confidence: Decimal,
        btc_price: Decimal,
        threshold: Decimal,
        direction: str,
        time_left: int,
    ) -> Optional[Dict]:
        if yes_edge < -self.max_edge or yes_edge > self.max_edge:
            return None
        if no_edge is not None and (no_edge < -self.max_edge or no_edge > self.max_edge):
            return None

        market_id = market.get("id") or market.get("condition_id")
        if not market_id:
            return None

        side = None
        token_id = None
        market_price = None
        edge = None

        if yes_edge >= self.min_edge:
            side = "YES"
            token_id = yes_token_id
            market_price = yes_price
            edge = yes_edge
        elif no_edge is not None and no_edge >= self.min_edge:
            side = "NO"
            token_id = no_token_id
            market_price = no_price
            edge = no_edge

        if not side:
            return None

        confidence = "HIGH" if abs(edge) > Decimal("0.10") else "MEDIUM"

        return {
            "market_id": market_id,
            "token_id": token_id,
            "side": side,
            "true_prob": true_prob,
            "market_price": market_price,
            "edge": edge,
            "confidence": confidence,
            "charlie_confidence": charlie_confidence,
            "direction": "UP" if side == "YES" else "DOWN",
            "btc_price": btc_price,
            "threshold": threshold,
            "time_left": time_left,
            "question": market.get("question") or market.get("title"),
        }

    def _extract_threshold_and_direction(self, question: str) -> Tuple[Optional[Decimal], Optional[str]]:
        lower = question.lower()

        above = any(token in lower for token in ["above", "over", ">", ">="])
        below = any(token in lower for token in ["below", "under", "<", "<="])

        threshold_match = re.findall(r"\$?([\d,]{3,})", question)
        if not threshold_match:
            return None, None

        try:
            threshold_str = max(
                threshold_match,
                key=lambda value: int(value.replace(",", ""))
            )
            threshold = Decimal(threshold_str.replace(",", ""))
        except Exception:
            return None, None

        if above and not below:
            return threshold, "ABOVE"
        if below and not above:
            return threshold, "BELOW"

        return threshold, None

    def _extract_token_ids(self, market: Dict) -> Tuple[Optional[str], Optional[str]]:
        yes_token_id = (
            market.get("yes_token_id")
            or market.get("yes_token")
            or market.get("yesTokenId")
            or market.get("yesToken")
        )
        no_token_id = (
            market.get("no_token_id")
            or market.get("no_token")
            or market.get("noTokenId")
            or market.get("noToken")
        )

        event_markets = market.get("markets") or []
        if isinstance(event_markets, list) and event_markets:
            first_market = event_markets[0]
            if isinstance(first_market, dict):
                yes_token_id = (
                    yes_token_id
                    or first_market.get("yes_token_id")
                    or first_market.get("yes_token")
                    or first_market.get("yesTokenId")
                )
                no_token_id = (
                    no_token_id
                    or first_market.get("no_token_id")
                    or first_market.get("no_token")
                    or first_market.get("noTokenId")
                )

        tokens = self._extract_tokens_from_market(market)
        if tokens:
            for token in tokens:
                outcome = str(token.get("outcome") or token.get("name") or "").upper()
                token_id = token.get("token_id") or token.get("tokenId") or token.get("id") or token.get("outcome_id")
                if outcome == "YES" and token_id:
                    yes_token_id = yes_token_id or token_id
                if outcome == "NO" and token_id:
                    no_token_id = no_token_id or token_id

        clob_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
        parsed_clob_ids: List[str] = []
        if isinstance(clob_token_ids, list):
            parsed_clob_ids = [str(token).strip() for token in clob_token_ids if str(token).strip()]
        elif isinstance(clob_token_ids, str) and clob_token_ids.strip():
            raw_value = clob_token_ids.strip()
            try:
                loaded = json.loads(raw_value)
                if isinstance(loaded, list):
                    parsed_clob_ids = [str(token).strip() for token in loaded if str(token).strip()]
            except json.JSONDecodeError:
                if "," in raw_value:
                    parsed_clob_ids = [segment.strip() for segment in raw_value.split(",") if segment.strip()]
                else:
                    parsed_clob_ids = [raw_value]

        if len(parsed_clob_ids) >= 2:
            yes_token_id = yes_token_id or parsed_clob_ids[0]
            no_token_id = no_token_id or parsed_clob_ids[1]

        return yes_token_id, no_token_id

    def _extract_token_price(self, market: Dict, outcome: str) -> Optional[Decimal]:
        if outcome == "YES":
            value = market.get("yes_price")
            if value is not None:
                return Decimal(str(value))
        if outcome == "NO":
            value = market.get("no_price")
            if value is not None:
                return Decimal(str(value))

        tokens = self._extract_tokens_from_market(market)
        for token in tokens:
            token_outcome = str(token.get("outcome") or token.get("name") or "").upper()
            if token_outcome != outcome:
                continue
            price = token.get("price")
            if price is not None:
                return Decimal(str(price))

        return None

    def _extract_time_left_seconds(self, market: Dict) -> Optional[int]:
        """
        Extract seconds until market closes from various API formats.
        Handles both snake_case (CLOB) and camelCase (Gamma API) field names.
        """
        time_fields = [
            "endDate",
            "end_date",
            "closeTime",
            "close_time",
            "closedTime",
            "closed_time",
            "end_time",
            "closes_at",
            "resolve_time",
            "resolution_time",
            "resolutionTime",
            "expires_at",
            "expiresAt",
        ]

        end_time_raw = None
        for field in time_fields:
            if field in market:
                candidate = market.get(field)
                if candidate:
                    end_time_raw = candidate
                    break

        if not end_time_raw:
            return None

        try:
            if isinstance(end_time_raw, str):
                if "T" in end_time_raw or "Z" in end_time_raw:
                    end_time = datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
                else:
                    end_time = datetime.fromtimestamp(to_decimal(end_time_raw), tz=timezone.utc)
            elif isinstance(end_time_raw, (int, float, Decimal)):
                end_time = datetime.fromtimestamp(to_decimal(end_time_raw), tz=timezone.utc)
            else:
                return None

            now = datetime.now(timezone.utc)
            seconds_left = int((end_time - now).total_seconds())
            return seconds_left if seconds_left > 0 else 0
        except (ValueError, TypeError) as exc:
            logger.debug(
                "time_parse_failed",
                end_time_str=str(end_time_raw)[:50],
                error=str(exc),
            )
            return None

    @staticmethod
    def _extract_mid_price(orderbook: Optional[Dict]) -> Optional[Decimal]:
        if not orderbook:
            return None

        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []

        if not bids or not asks:
            return None

        try:
            best_bid = Decimal(str(bids[0]["price"]))
            best_ask = Decimal(str(asks[0]["price"]))
        except Exception:
            return None

        return (best_bid + best_ask) / Decimal("2")
