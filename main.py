#!/usr/bin/env python3
"""
Polymarket Trading Bot - Main Application

Production-grade trading system with:
- Multiple trading strategies
- Real-time market data
- Risk management
- Health monitoring
- Paper trading mode

Usage:
    python main.py --config config/production.yaml --mode paper
    python main.py --config config/production.yaml --mode live
"""

import asyncio
import signal
import sys
import argparse
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from decimal import Decimal
import yaml
import structlog

# Import core components
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from database.ledger_async import AsyncLedger
from services.execution_service_v2 import ExecutionServiceV2
from services.health_monitor_v2 import HealthMonitorV2
from risk.circuit_breaker_v2 import CircuitBreakerV2
from security.secrets_manager import SecretsManager, get_secrets_manager
from validation.models import TradingConfig
from strategies.latency_arbitrage_btc import LatencyArbitrageEngine as MultiTimeframeLatencyArbitrageEngine
from utils.decimal_helpers import quantize_quantity, to_decimal

# Charlie integration — new modules
from state.order_store import OrderStore, OrderState
from risk.performance_tracker import PerformanceTracker
from risk.kelly_sizing import KellySizer
from integrations.charlie_booster import CharliePredictionGate, TradeRecommendation
from config_production import (
    CHARLIE_CONFIG,
    KELLY_CONFIG,
    ORDER_STORE_CONFIG,
    PERFORMANCE_TRACKER_CONFIG,
    REGIME_RISK_OVERRIDES,
    GLOBAL_RISK_BUDGET,
    STARTING_CAPITAL,
)
from services.portfolio_state import PortfolioState
from services.do_not_trade import DoNotTradeRegistry


def _build_model_feedback_callback():
    """
    Return a callable ``(was_correct: bool, order: dict) -> None`` that
    propagates settled-trade outcomes back into Charlie's ensemble accuracy
    weights **per model**, using the ``model_votes`` JSON stored at order
    creation time.

    Per-model attribution logic
    ---------------------------
    At order creation, ``record_order_created`` stores the full model_votes
    dict (e.g. {"random_forest": "BUY", "svm": "HOLD", ...}) in the
    ``model_votes`` column.  On settlement we know the actual direction
    (profit = UP / YES, loss = DOWN / NO).  Each model that voted in the
    correct direction gets ``was_correct=True``; others get ``False``.
    This lets the EWMA accuracy tracker differentiate models that are
    systematically better or worse, rather than giving all five the same
    lesson.

    Falls back to uniform update if ``model_votes`` is absent (legacy rows).
    """
    import json as _json
    import logging as _logging

    _MODEL_NAMES = (
        "random_forest", "xgboost", "neural_network", "svm", "lstm",
    )
    _log = _logging.getLogger(__name__)

    def _get_engine():
        import sys, os
        charlie_root = os.getenv("CHARLIE_PATH", "")
        if charlie_root and charlie_root not in sys.path:
            sys.path.insert(0, charlie_root)
        from src.api import signals as _signals_mod  # type: ignore
        return _signals_mod._ensemble_engine

    def _callback(was_correct: bool, order: dict = None) -> None:
        try:
            engine = _get_engine()
            if engine is None:
                return

            # Try per-model attribution via stored model_votes
            model_votes = None
            if order is not None:
                raw_votes = order.get("model_votes")
                if raw_votes:
                    try:
                        model_votes = _json.loads(raw_votes) if isinstance(raw_votes, str) else raw_votes
                    except Exception:
                        model_votes = None

            if model_votes and isinstance(model_votes, dict):
                # Determine winning direction: positive PnL = YES/BUY was correct.
                correct_vote = "BUY" if was_correct else "SELL"
                for model_name in _MODEL_NAMES:
                    vote = model_votes.get(model_name)
                    if vote is None:
                        continue
                    model_correct = (vote == correct_vote)
                    engine.update_model_performance(model_name, model_correct)
                _log.debug(
                    "per_model_feedback_dispatched",
                    correct_direction=correct_vote,
                    votes=model_votes,
                )
            else:
                # Legacy fallback: uniform update when no model_votes are stored.
                for model_name in _MODEL_NAMES:
                    engine.update_model_performance(model_name, was_correct)
                _log.debug(
                    "uniform_model_feedback_dispatched",
                    was_correct=was_correct,
                    reason="no_model_votes_in_order",
                )

        except Exception as exc:
            # Feedback is best-effort — never crash a settled-trade update
            import logging
            logging.getLogger(__name__).warning(
                "model_feedback_callback_error", error=str(exc)
            )

    return _callback

logger = structlog.get_logger(__name__)


class TradingSystem:
    """
    Main trading system orchestrator.
    
    Manages all components and coordinates trading operations.
    """
    
    def __init__(self, config: dict):
        """
        Initialize trading system.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.running = False
        self.shutdown_event = asyncio.Event()
        
        # Components (initialized in start())
        self.secrets_manager: Optional[SecretsManager] = None
        self.ledger: Optional[AsyncLedger] = None
        self.api_client: Optional[PolymarketClientV2] = None
        self.websocket: Optional[BinanceWebSocketV2] = None
        self.execution: Optional[ExecutionServiceV2] = None
        self.health_monitor: Optional[HealthMonitorV2] = None
        self.circuit_breaker: Optional[CircuitBreakerV2] = None
        self.strategy_engine: Optional[MultiTimeframeLatencyArbitrageEngine] = None
        self.strategy_scan_lock = asyncio.Lock()
        self.last_strategy_scan_at = 0.0

        # Charlie integration components
        self.order_store: Optional[OrderStore] = None
        self.performance_tracker: Optional[PerformanceTracker] = None
        self.kelly_sizer: Optional[KellySizer] = None
        self.charlie_gate: Optional[CharliePredictionGate] = None
        self.portfolio_state: Optional[PortfolioState] = None
        self.do_not_trade: DoNotTradeRegistry = DoNotTradeRegistry(
            path="data/do_not_trade.json",
            auto_load=True,
        )
        self.last_discovered_markets = []
        startup_config = config.get('startup', {})
        self.init_timeout_seconds = float(startup_config.get('component_timeout_seconds', 25.0))
        self.network_timeout_seconds = float(startup_config.get('network_timeout_seconds', 20.0))
        self.loop_tick_seconds = float(startup_config.get('loop_tick_seconds', 10.0))
        self.market_probe_interval_seconds = float(startup_config.get('market_probe_interval_seconds', 30.0))
        self.market_probe_limit = int(startup_config.get('market_probe_limit', 10))
        self.strategy_scan_min_interval_seconds = float(startup_config.get('strategy_scan_min_interval_seconds', 2.0))
        self.strategy_scan_timeout_seconds = float(startup_config.get('strategy_scan_timeout_seconds', 30.0))
        self.last_market_probe_at = 0.0
        self.last_heartbeat_at = 0.0
        self.start_time = asyncio.get_event_loop().time()
        
        logger.info(
            "trading_system_initialized",
            environment=config.get('environment', 'unknown'),
            paper_trading=config.get('trading', {}).get('paper_trading', True),
            init_timeout_seconds=self.init_timeout_seconds,
            network_timeout_seconds=self.network_timeout_seconds,
            loop_tick_seconds=self.loop_tick_seconds,
            strategy_scan_min_interval_seconds=self.strategy_scan_min_interval_seconds,
        )

    async def _await_step(self, step_name: str, coro, timeout_seconds: Optional[float] = None):
        timeout = float(timeout_seconds if timeout_seconds is not None else self.init_timeout_seconds)
        logger.info("startup_step_begin", step=step_name, timeout_seconds=timeout)
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
            logger.info("startup_step_success", step=step_name)
            return result
        except asyncio.TimeoutError as e:
            logger.error("startup_step_timeout", step=step_name, timeout_seconds=timeout)
            raise TimeoutError(f"{step_name} timed out after {timeout}s") from e
        except Exception as e:
            logger.error(
                "startup_step_failed",
                step=step_name,
                error=str(e),
                error_type=type(e).__name__
            )
            raise

    async def _safe_await(self, label: str, coro, timeout_seconds: Optional[float] = None, default=None):
        timeout = float(timeout_seconds if timeout_seconds is not None else self.network_timeout_seconds)
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("operation_timeout", label=label, timeout_seconds=timeout)
            return default
        except Exception as e:
            logger.warning(
                "operation_failed",
                label=label,
                error=str(e),
                error_type=type(e).__name__
            )
            return default

    async def _market_discovery_probe(self):
        if not self.api_client:
            logger.warning("market_probe_skipped", reason="api_client_unavailable")
            return

        markets = await self._safe_await(
            "api_client.get_markets.active",
            self.api_client.get_markets(active=True, limit=self.market_probe_limit),
            timeout_seconds=self.network_timeout_seconds,
            default=[]
        )

        if not markets:
            markets = await self._safe_await(
                "api_client.get_active_markets",
                self.api_client.get_active_markets(limit=self.market_probe_limit),
                timeout_seconds=max(self.network_timeout_seconds, 30.0),
                default=[]
            )

        if not markets:
            if self.last_discovered_markets:
                logger.warning(
                    "market_probe_cache_reused",
                    cached_count=len(self.last_discovered_markets)
                )
                markets = self.last_discovered_markets
            else:
                logger.warning("market_probe_empty", limit=self.market_probe_limit)
                return

        self.last_discovered_markets = [m for m in markets if isinstance(m, dict)]

        if not self.last_discovered_markets:
            logger.warning("market_probe_empty", limit=self.market_probe_limit)
            return

        sample_identifiers = []
        for market in self.last_discovered_markets[:3]:
            if isinstance(market, dict):
                sample_identifiers.append(
                    market.get('slug')
                    or market.get('question')
                    or market.get('id')
                )

        logger.info(
            "market_probe_success",
            discovered_count=len(self.last_discovered_markets),
            sample=sample_identifiers
        )

    async def _on_price_update(self, symbol: str, price_data) -> None:
        try:
            if symbol != "BTC":
                return

            logger.info(
                "price_update",
                symbol=symbol,
                price=str(getattr(price_data, "price", None)),
                timestamp=str(getattr(price_data, "timestamp", None)),
            )

            await self._run_strategy_scan(trigger="price_tick")
        except Exception as e:
            logger.warning(
                "price_update_callback_failed",
                error=str(e),
                error_type=type(e).__name__
            )

    async def _run_strategy_scan(self, trigger: str) -> None:
        if not self.strategy_engine:
            return

        now = asyncio.get_event_loop().time()
        if (now - self.last_strategy_scan_at) < self.strategy_scan_min_interval_seconds:
            return

        if self.strategy_scan_lock.locked():
            return

        async with self.strategy_scan_lock:
            self.last_strategy_scan_at = now
            logger.info("strategy_scan_begin", trigger=trigger)

            try:
                opportunity = await asyncio.wait_for(
                    self.strategy_engine.scan_opportunities(),
                    timeout=self.strategy_scan_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "strategy_scan_timeout",
                    trigger=trigger,
                    timeout_seconds=self.strategy_scan_timeout_seconds,
                )
                return
            except Exception as e:
                logger.error(
                    "strategy_scan_failed",
                    trigger=trigger,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                return

            if not opportunity:
                logger.info("strategy_scan_complete", trigger=trigger, opportunity_found=False)
                return

            edge = opportunity.get("edge")
            spread_bps = None
            try:
                spread_bps = float(Decimal(str(edge)) * Decimal("10000"))
            except Exception:
                spread_bps = None

            logger.info(
                "arbitrage_opportunity_detected",
                trigger=trigger,
                market_id=opportunity.get("market_id"),
                side=opportunity.get("side"),
                timeframe=opportunity.get("timeframe"),
                btc_price=str(opportunity.get("btc_price")),
                market_price=str(opportunity.get("market_price")),
                edge=str(opportunity.get("edge")),
                spread_bps=spread_bps,
            )

            if self.config.get('trading', {}).get('paper_trading', True):
                logger.info(
                    "paper_trade_signal",
                    market_id=opportunity.get("market_id"),
                    token_id=opportunity.get("token_id"),
                    side=opportunity.get("side"),
                    confidence=opportunity.get("confidence"),
                    edge=str(opportunity.get("edge")),
                    trigger=trigger,
                )

            await self._execute_opportunity(opportunity=opportunity, trigger=trigger)

    def _resolve_opportunity_confidence(self, confidence_value: Any) -> Decimal:
        if confidence_value is None:
            return Decimal("0.6")
        if isinstance(confidence_value, Decimal):
            return max(Decimal("0.01"), min(Decimal("0.99"), confidence_value))
        if isinstance(confidence_value, str):
            normalized = confidence_value.strip().upper()
            if normalized == "HIGH":
                return Decimal("0.8")
            if normalized == "MEDIUM":
                return Decimal("0.65")
            if normalized == "LOW":
                return Decimal("0.55")
        try:
            parsed = to_decimal(confidence_value)
            return max(Decimal("0.01"), min(Decimal("0.99"), parsed))
        except Exception:
            return Decimal("0.6")

    async def _execute_opportunity(self, opportunity: Dict[str, Any], trigger: str) -> None:
        if not self.execution or not self.ledger:
            logger.warning(
                "opportunity_skipped",
                reason="execution_or_ledger_unavailable",
                trigger=trigger,
            )
            return

        market_id = str(opportunity.get("market_id") or "").strip()
        token_id = str(opportunity.get("token_id") or "").strip()
        side = str(opportunity.get("side") or "").upper()
        if not market_id or not token_id:
            logger.warning(
                "opportunity_skipped",
                reason="missing_market_or_token_id",
                market_id=market_id or None,
                token_id=token_id or None,
                trigger=trigger,
            )
            return
        if side not in {"YES", "NO"}:
            logger.warning(
                "opportunity_skipped",
                reason="invalid_opportunity_side",
                side=side,
                market_id=market_id,
                trigger=trigger,
            )
            return

        # --- DoNotTrade check — before any API calls or risk calculations ---
        if self.do_not_trade.is_blocked(market_id):
            logger.info(
                "opportunity_skipped",
                reason="do_not_trade_registry",
                market_id=market_id,
                entry=self.do_not_trade.all_blocked().get(market_id, {}),
                trigger=trigger,
            )
            return

        try:
            edge = to_decimal(opportunity.get("edge"))
        except Exception:
            logger.warning(
                "opportunity_skipped",
                reason="invalid_edge",
                market_id=market_id,
                trigger=trigger,
            )
            return

        price_raw = opportunity.get("market_price")
        if price_raw is None:
            price_raw = opportunity.get("price")
        if price_raw is None:
            logger.warning(
                "opportunity_skipped",
                reason="missing_market_price",
                market_id=market_id,
                token_id=token_id,
                trigger=trigger,
            )
            return

        try:
            price = to_decimal(price_raw)
        except Exception:
            logger.warning(
                "opportunity_skipped",
                reason="invalid_market_price",
                market_id=market_id,
                token_id=token_id,
                trigger=trigger,
            )
            return

        trading_cfg = self.config.get("trading", {})
        strategy_cfg = self.config.get("strategies", {}).get("latency_arb", {})

        min_price = to_decimal(trading_cfg.get("min_price", "0.01"))
        max_price = to_decimal(trading_cfg.get("max_price", "0.99"))
        if not (min_price <= price <= max_price):
            logger.warning(
                "opportunity_skipped",
                reason="price_out_of_bounds",
                market_id=market_id,
                token_id=token_id,
                price=str(price),
                min_price=str(min_price),
                max_price=str(max_price),
                trigger=trigger,
            )
            return

        equity = await self._safe_await(
            "ledger.get_equity.execute_opportunity",
            self.ledger.get_equity(),
            default=Decimal("0"),
        )
        if not isinstance(equity, Decimal):
            equity = to_decimal(equity)
        if equity <= Decimal("0"):
            logger.warning(
                "opportunity_skipped",
                reason="non_positive_equity",
                market_id=market_id,
                trigger=trigger,
            )
            return

        max_position_pct = to_decimal(
            strategy_cfg.get(
                "max_position_size_pct",
                trading_cfg.get("max_position_size_pct", "5.0"),
            )
        )
        min_position_size = to_decimal(trading_cfg.get("min_position_size", "1.00"))
        max_order_size = to_decimal(trading_cfg.get("max_order_size", "1000.00"))

        raw_position_value = equity * (max_position_pct / Decimal("100"))
        position_value = max(raw_position_value, min_position_size)
        position_value = min(position_value, max_order_size, equity)
        if position_value < min_position_size:
            logger.warning(
                "opportunity_skipped",
                reason="position_value_below_minimum",
                market_id=market_id,
                position_value=str(position_value),
                min_position_size=str(min_position_size),
                trigger=trigger,
            )
            return

        quantity = quantize_quantity(position_value / price)
        if quantity <= Decimal("0"):
            logger.warning(
                "opportunity_skipped",
                reason="quantity_too_small",
                market_id=market_id,
                position_value=str(position_value),
                price=str(price),
                trigger=trigger,
            )
            return

        order_value = quantize_quantity(quantity * price)
        if order_value < min_position_size:
            logger.warning(
                "opportunity_skipped",
                reason="order_value_below_minimum",
                market_id=market_id,
                token_id=token_id,
                order_value=str(order_value),
                min_position_size=str(min_position_size),
                trigger=trigger,
            )
            return

        position_size_pct = float((order_value / equity) * Decimal("100"))
        if self.circuit_breaker:
            can_trade = await self._safe_await(
                "circuit_breaker.can_trade.execute_opportunity",
                self.circuit_breaker.can_trade(equity, position_size_pct=position_size_pct),
                default=False,
            )
            if not can_trade:
                logger.warning(
                    "risk_rejected",
                    reason="circuit_breaker_blocked",
                    market_id=market_id,
                    token_id=token_id,
                    position_size_pct=position_size_pct,
                    trigger=trigger,
                )
                return

        confidence = self._resolve_opportunity_confidence(opportunity.get("confidence"))

        # --- Charlie gate: mandatory signal check before any order -----------
        charlie_rec: Optional[TradeRecommendation] = None
        charlie_p_win: Optional[Decimal] = None
        charlie_conf_dec: Optional[Decimal] = None
        charlie_regime: Optional[str] = None

        if self.charlie_gate is not None:
            # Map opportunity symbol to Charlie vocab (default BTC)
            opp_symbol = str(opportunity.get("symbol") or opportunity.get("btc_symbol") or "BTC")
            charlie_rec = await self._safe_await(
                "charlie_gate.evaluate_market",
                self.charlie_gate.evaluate_market(
                    market_id=market_id,
                    market_price=price,
                    symbol=opp_symbol,
                    timeframe="15m",
                    bankroll=equity,
                    override_win_rate=(
                        self.performance_tracker.get_rolling_win_rate(20)
                        if self.performance_tracker is not None
                        else None
                    ),
                ),
                timeout_seconds=10.0,
                default=None,
            )

            if charlie_rec is None:
                logger.info(
                    "order_blocked_no_charlie_signal",
                    market_id=market_id,
                    trigger=trigger,
                )
                return

            # Adopt Charlie's recommended side and Kelly size
            side = charlie_rec.side
            quantity = quantize_quantity(charlie_rec.size / price) if price > Decimal("0") else quantity
            order_value = quantize_quantity(quantity * price)
            charlie_p_win  = Decimal(str(charlie_rec.p_win))
            charlie_conf_dec = Decimal(str(charlie_rec.confidence))
            charlie_regime = charlie_rec.regime

            # --- Regime-based position-size multiplier ----------------------
            # Scale down (or up) Kelly size according to the detected technical
            # regime.  HIGH_VOL → 50% of Kelly; etc.  UNKNOWN means we didn't
            # have enough features to classify — pass-through at 1.0× rather
            # than penalising the trade.  Never allows the multiplier to INCREASE
            # size beyond the hard Kelly cap.
            technical_regime = getattr(charlie_rec, "technical_regime", "UNKNOWN")
            regime_mult = REGIME_RISK_OVERRIDES.get(technical_regime, Decimal("1.0"))
            regime_mult = min(regime_mult, Decimal("1.0"))  # cap at 1× regardless of config
            if regime_mult < Decimal("1.0"):
                quantity = quantize_quantity(quantity * regime_mult)
                order_value = quantize_quantity(quantity * price)
                logger.info(
                    "regime_size_adjustment",
                    market_id=market_id,
                    technical_regime=technical_regime,
                    multiplier=str(regime_mult),
                    adjusted_order_value=str(order_value),
                )

            # --- Global + per-market risk budget check ----------------------
            # Refresh the portfolio snapshot so we have an up-to-date view
            # of total exposure before committing this trade.
            if self.portfolio_state is not None:
                self.portfolio_state.update_equity(equity)
                await self._safe_await(
                    "portfolio_state.refresh.pre_trade",
                    self.portfolio_state.refresh(),
                )
                if not self.portfolio_state.within_global_budget(order_value):
                    logger.warning(
                        "order_blocked_global_risk_budget_exceeded",
                        market_id=market_id,
                        proposed_size=str(order_value),
                        total_exposure=str(self.portfolio_state.total_exposure),
                        equity=str(equity),
                        trigger=trigger,
                    )
                    return
                if not self.portfolio_state.within_market_budget(market_id, order_value):
                    logger.warning(
                        "order_blocked_per_market_budget_exceeded",
                        market_id=market_id,
                        proposed_size=str(order_value),
                        market_exposure=str(self.portfolio_state.exposure_for_market(market_id)),
                        equity=str(equity),
                        trigger=trigger,
                    )
                    return

            if order_value < min_position_size:
                logger.info(
                    "order_blocked_kelly_size_too_small",
                    market_id=market_id,
                    order_value=str(order_value),
                    min_position_size=str(min_position_size),
                    trigger=trigger,
                )
                return
        else:
            logger.warning(
                "charlie_gate_not_initialised — proceeding without Charlie (should not happen in production)",
                market_id=market_id,
            )

        metadata = {
            "trigger": trigger,
            "outcome": side,
            "direction": str(opportunity.get("direction") or ("UP" if side == "YES" else "DOWN")),
            "edge": str(edge),
            "confidence": str(confidence),
            "question": str(opportunity.get("question") or ""),
            "btc_price": str(opportunity.get("btc_price")) if opportunity.get("btc_price") is not None else None,
            "charlie_p_win": str(charlie_p_win) if charlie_p_win is not None else None,
            "charlie_confidence": str(charlie_conf_dec) if charlie_conf_dec is not None else None,
            "charlie_regime": charlie_regime,
            "charlie_edge": str(charlie_rec.edge) if charlie_rec is not None else None,
            "charlie_implied_prob": str(charlie_rec.implied_prob) if charlie_rec is not None else None,
        }

        # Write CREATED row to unified order ledger before sending to exchange.
        # Stores model_votes so per-model feedback works on settlement.
        pre_order_id = f"pre_{market_id}_{token_id}_{int(asyncio.get_event_loop().time()*1000)}"
        if self.ledger is not None:
            await self._safe_await(
                "ledger.record_order_created",
                self.ledger.record_order_created(
                    order_id=pre_order_id,
                    market_id=market_id,
                    token_id=token_id,
                    outcome=side,
                    side="BUY",
                    size=order_value,
                    price=price,
                    charlie_p_win=charlie_p_win,
                    charlie_conf=charlie_conf_dec,
                    charlie_regime=charlie_regime,
                    strategy="latency_arbitrage_btc",
                    model_votes=charlie_rec.model_votes if charlie_rec is not None else None,
                    notes=charlie_rec.reason if charlie_rec else None,
                ),
            )
        # Legacy order_store write (transition period — kept until order_store is removed)
        if self.order_store is not None:
            await self._safe_await(
                "order_store.upsert_pre_order",
                self.order_store.upsert_order(
                    order_id=pre_order_id,
                    market_id=market_id,
                    token_id=token_id,
                    outcome=side,
                    side="BUY",
                    size=order_value,
                    price=price,
                    state=OrderState.CREATED,
                    charlie_p_win=charlie_p_win,
                    charlie_conf=charlie_conf_dec,
                    charlie_regime=charlie_regime,
                    strategy="latency_arbitrage_btc",
                    notes=charlie_rec.reason if charlie_rec else None,
                ),
            )

        logger.info(
            "order_submission_attempt",
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            outcome=side,
            quantity=str(quantity),
            price=str(price),
            order_value=str(order_value),
            edge=str(edge),
            charlie_p_win=str(charlie_p_win),
            charlie_confidence=str(charlie_conf_dec),
            charlie_regime=charlie_regime,
            trigger=trigger,
        )

        result = await self.execution.place_order_with_risk_check(
            trade_delta=order_value,
            strategy="latency_arbitrage_btc",
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            quantity=quantity,
            price=price,
            metadata=metadata,
        )

        # --- Update unified order ledger with exchange result -------------------
        if result is not None:
            exchange_order_id = getattr(result, "order_id", None) or pre_order_id or ""
            if result.success and exchange_order_id:
                filled_qty = getattr(result, "filled_quantity", None) or getattr(result, "filled_size", None)
                new_state_str = "FILLED" if (filled_qty and filled_qty > Decimal("0")) else "SUBMITTED"
                # If exchange returned a new ID, create a proper row for it
                if exchange_order_id != pre_order_id and self.ledger is not None:
                    await self._safe_await(
                        "ledger.record_order_submitted",
                        self.ledger.record_order_created(
                            order_id=exchange_order_id,
                            market_id=market_id,
                            token_id=token_id,
                            outcome=side,
                            side="BUY",
                            size=order_value,
                            price=price,
                            charlie_p_win=charlie_p_win,
                            charlie_conf=charlie_conf_dec,
                            charlie_regime=charlie_regime,
                            strategy="latency_arbitrage_btc",
                            model_votes=charlie_rec.model_votes if charlie_rec is not None else None,
                            notes=charlie_rec.reason if charlie_rec else None,
                        ),
                    )
                if self.ledger is not None:
                    await self._safe_await(
                        "ledger.transition_order_state_submitted",
                        self.ledger.transition_order_state(
                            exchange_order_id, new_state_str
                        ),
                    )
                # Legacy order_store update (transition period)
                if self.order_store is not None:
                    from state.order_store import OrderState as _OS
                    _new_os = _OS.FILLED if new_state_str == "FILLED" else _OS.SUBMITTED
                    if exchange_order_id != pre_order_id:
                        await self._safe_await(
                            "order_store.upsert_submitted",
                            self.order_store.upsert_order(
                                order_id=exchange_order_id,
                                market_id=market_id,
                                token_id=token_id,
                                outcome=side,
                                side="BUY",
                                size=order_value,
                                price=price,
                                state=_new_os,
                                charlie_p_win=charlie_p_win,
                                charlie_conf=charlie_conf_dec,
                                charlie_regime=charlie_regime,
                                strategy="latency_arbitrage_btc",
                                notes=charlie_rec.reason if charlie_rec else None,
                            ),
                        )
                    else:
                        await self._safe_await(
                            "order_store.transition_submitted",
                            self.order_store.transition_state(exchange_order_id, _new_os),
                        )
            elif not result.success and pre_order_id:
                if self.ledger is not None:
                    await self._safe_await(
                        "ledger.transition_order_state_error",
                        self.ledger.transition_order_state(
                            pre_order_id, "ERROR",
                            notes=str(getattr(result, "error", "unknown")),
                        ),
                    )
                if self.order_store is not None:
                    await self._safe_await(
                        "order_store.transition_error",
                        self.order_store.transition_state(
                            pre_order_id, OrderState.ERROR,
                            notes=str(getattr(result, "error", "unknown"))
                        ),
                    )

        if not result.success:
            logger.error(
                "execution_failed",
                market_id=market_id,
                token_id=token_id,
                error=result.error,
                error_code=result.error_code,
                status=result.status.value if hasattr(result.status, "value") else str(result.status),
                trigger=trigger,
            )
            return

        logger.info(
            "order_submitted",
            order_id=result.order_id,
            market_id=market_id,
            token_id=token_id,
            trigger=trigger,
        )

        if result.filled_quantity and result.filled_quantity > Decimal("0"):
            logger.info(
                "order_filled",
                order_id=result.order_id,
                market_id=market_id,
                token_id=token_id,
                filled_quantity=str(result.filled_quantity),
                filled_price=str(result.filled_price),
                fees=str(result.fees),
                trigger=trigger,
            )
            logger.info(
                "paper_trade_executed" if self.config.get("trading", {}).get("paper_trading", True) else "trade_executed",
                order_id=result.order_id,
                market_id=market_id,
                token_id=token_id,
                outcome=side,
                edge=str(edge),
                trigger=trigger,
            )
            logger.info(
                "position_opened",
                order_id=result.order_id,
                market_id=market_id,
                token_id=token_id,
                quantity=str(result.filled_quantity),
                avg_price=str(result.filled_price),
                trigger=trigger,
            )
    
    async def initialize_components(self):
        """Initialize all system components."""
        logger.info("initializing_components")
        
        try:
            paper_trading = self.config.get('trading', {}).get('paper_trading', True)

            # 1. Secrets Manager
            secrets_config = self.config.get('secrets', {})
            secrets_backend = secrets_config.get('backend', 'env')
            if paper_trading and secrets_backend == 'local':
                logger.warning(
                    "paper_mode_overriding_secrets_backend",
                    from_backend=secrets_backend,
                    to_backend='env'
                )
                secrets_backend = 'env'

            logger.info("component_construct_begin", component="secrets_manager")
            self.secrets_manager = SecretsManager(
                backend=secrets_backend,
                aws_region=secrets_config.get('aws_region', 'us-east-1'),
                local_secrets_path=secrets_config.get('local_secrets_path', '.secrets.enc')
            )
            logger.info("component_construct_success", component="secrets_manager")
            
            # 2. Database/Ledger
            db_config = self.config.get('database', {})
            db_path = db_config.get('path', 'data/trading.db')
            
            # Ensure directory exists
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            
            logger.info("component_construct_begin", component="ledger")
            self.ledger = AsyncLedger(
                db_path=db_path,
                pool_size=db_config.get('pool_size', 5),
                cache_ttl=db_config.get('cache_ttl_seconds', 5)
            )
            logger.info("component_construct_success", component="ledger")
            await self._await_step("ledger.pool.initialize", self.ledger.pool.initialize())
            logger.info("ledger_initialized", path=db_path)
            
            # Initialize with capital if needed
            equity = await self._await_step("ledger.get_equity", self.ledger.get_equity())
            if equity == Decimal('0'):
                initial_capital = Decimal(str(self.config.get('trading', {}).get('initial_capital', 10000)))
                await self._await_step(
                    "ledger.record_deposit",
                    self.ledger.record_deposit(initial_capital, "Initial capital")
                )
                logger.info("initial_capital_deposited", amount=initial_capital)
            
            # 3. API Client
            api_config = self.config.get('api', {}).get('polymarket', {})
            
            # Get API credentials from secrets
            api_key = await self._await_step(
                "secrets.get.polymarket_api_key",
                self.secrets_manager.get_secret('polymarket_api_key')
            )
            private_key = None
            if not paper_trading:
                private_key = await self._await_step(
                    "secrets.get.polymarket_private_key",
                    self.secrets_manager.get_secret('polymarket_private_key')
                )
            
            logger.info("component_construct_begin", component="api_client")
            self.api_client = PolymarketClientV2(
                api_key=api_key,
                private_key=private_key,
                paper_trading=paper_trading,
                rate_limit=api_config.get('rate_limit', 8.0),
                timeout=api_config.get('timeout_seconds', 10.0),
                max_retries=api_config.get('max_retries', 3)
            )
            logger.info("component_construct_success", component="api_client", paper_trading=paper_trading)
            
            # 4. WebSocket
            ws_config = self.config.get('api', {}).get('binance', {})
            symbols = self.config.get('markets', {}).get('crypto_symbols', ['BTC', 'ETH'])
            
            logger.info("component_construct_begin", component="websocket")
            self.websocket = BinanceWebSocketV2(
                symbols=symbols,
                on_price_update=self._on_price_update,
                heartbeat_interval=ws_config.get('heartbeat_interval', 30.0),
                max_reconnect_delay=ws_config.get('max_reconnect_delay', 60.0),
                message_queue_size=ws_config.get('message_queue_size', 1000),
                connect_retries=ws_config.get('connect_retries', 3),
                connect_retry_delay=ws_config.get('connect_retry_delay_seconds', 2.0),
                startup_health_grace_seconds=ws_config.get('startup_health_grace_seconds', 90.0),
            )
            logger.info("component_construct_success", component="websocket")
            ws_started = await self._await_step(
                "websocket.start",
                self.websocket.start(),
                timeout_seconds=max(self.network_timeout_seconds, 15.0),
            )
            if not ws_started:
                raise RuntimeError("WebSocket failed to start after configured retries")
            logger.info("websocket_initialized", symbols=symbols)
            
            # 5. Circuit Breaker
            risk_config = self.config.get('risk', {})
            current_equity = await self._await_step("ledger.get_equity_for_cb", self.ledger.get_equity())
            
            logger.info("component_construct_begin", component="circuit_breaker")
            self.circuit_breaker = CircuitBreakerV2(
                initial_equity=current_equity,
                max_drawdown_pct=risk_config.get('max_drawdown_pct', 15.0),
                max_loss_streak=risk_config.get('max_loss_streak', 5),
                daily_loss_limit_pct=risk_config.get('daily_loss_limit_pct', 10.0)
            )
            logger.info("component_construct_success", component="circuit_breaker", initial_equity=current_equity)
            
            # 6. Execution Service
            exec_config = self.config.get('execution', {})
            
            logger.info("component_construct_begin", component="execution_service")
            self.execution = ExecutionServiceV2(
                polymarket_client=self.api_client,
                ledger=self.ledger,
                config={
                    'timeout_seconds': exec_config.get('order_timeout_seconds', 60),
                    'fill_check_interval': exec_config.get('fill_check_interval_seconds', 2),
                    'max_order_age_seconds': exec_config.get('max_order_age_seconds', 3600),
                    'max_retries': self.config.get('api', {}).get('polymarket', {}).get('max_retries', 3),
                    'auto_block_slippage_bps': exec_config.get('auto_block_slippage_bps', 200),
                },
                do_not_trade_registry=self.do_not_trade,
            )
            logger.info("component_construct_success", component="execution_service")
            await self._await_step("execution_service.start", self.execution.start())

            # 7. Health Monitor
            monitor_config = self.config.get('monitoring', {})
            
            logger.info("component_construct_begin", component="health_monitor")
            self.health_monitor = HealthMonitorV2(
                check_interval=monitor_config.get('health_check_interval', 30.0),
                failure_threshold=monitor_config.get('failure_threshold', 3),
                alert_cooldown=monitor_config.get('alert_cooldown', 300.0),
                enable_auto_restart=monitor_config.get('auto_restart_enabled', True)
            )
            logger.info("component_construct_success", component="health_monitor")
            
            # Register components for health checks
            self.health_monitor.register_component(
                'api_client',
                self.api_client.health_check
            )
            self.health_monitor.register_component(
                'websocket',
                self.websocket.health_check
            )
            self.health_monitor.register_component(
                'database',
                self._check_database_health
            )
            
            await self._await_step("health_monitor.start", self.health_monitor.start())
            logger.info("health_monitor_initialized")

            await self._market_discovery_probe()

            # --- Charlie integration components ---
            # 8. Order Store
            order_store_path = ORDER_STORE_CONFIG.get("db_path", "data/orders_ledger.db")
            logger.info("component_construct_begin", component="order_store")
            self.order_store = OrderStore(db_path=order_store_path)
            await self._await_step("order_store.initialize", self.order_store.initialize())
            logger.info("component_construct_success", component="order_store", path=order_store_path)

            # 9. Kelly Sizer
            logger.info("component_construct_begin", component="kelly_sizer")
            self.kelly_sizer = KellySizer(config=KELLY_CONFIG)
            logger.info("component_construct_success", component="kelly_sizer")

            # 10. Charlie Prediction Gate
            charlie_min_edge = CHARLIE_CONFIG.get("min_edge", Decimal("0.05"))
            charlie_min_conf = CHARLIE_CONFIG.get("min_confidence", Decimal("0.60"))
            charlie_regimes = CHARLIE_CONFIG.get("allowed_regimes", None)
            charlie_timeout = float(CHARLIE_CONFIG.get("signal_timeout_seconds", 8.0))
            logger.info("component_construct_begin", component="charlie_gate")
            self.charlie_gate = CharliePredictionGate(
                kelly_sizer=self.kelly_sizer,
                min_edge=Decimal(str(charlie_min_edge)),
                min_confidence=Decimal(str(charlie_min_conf)),
                allowed_regimes=charlie_regimes,
                signal_timeout=charlie_timeout,
            )
            logger.info(
                "component_construct_success",
                component="charlie_gate",
                min_edge=str(charlie_min_edge),
                min_confidence=str(charlie_min_conf),
            )

            # 11. Performance Tracker
            # Pass ledger as the order store — it now has get_all_tracked_orders()
            # which reads from the unified order_tracking table (no split-brain).
            logger.info("component_construct_begin", component="performance_tracker")
            self.performance_tracker = PerformanceTracker(
                order_store=self.ledger,
                ledger=self.ledger,
                initial_capital=STARTING_CAPITAL,
                model_feedback_callback=_build_model_feedback_callback(),
            )
            await self._safe_await(
                "performance_tracker.refresh",
                self.performance_tracker.refresh(),
            )
            logger.info(
                "component_construct_success",
                component="performance_tracker",
                **{k: str(v) for k, v in self.performance_tracker.get_summary().items()},
            )

            # 12. Strategy Engine — wired with live charlie_gate, execution, kelly_sizer
            # Must be constructed AFTER steps 9–11 so all dependencies are real instances.
            strategy_cfg = self.config.get('strategies', {}).get('latency_arb', {})
            strategy_enabled = bool(strategy_cfg.get('enabled', True))
            if strategy_enabled:
                logger.info("component_construct_begin", component="latency_arb_strategy")
                self.strategy_engine = MultiTimeframeLatencyArbitrageEngine(
                    binance_ws=self.websocket,
                    polymarket_client=self.api_client,
                    charlie_predictor=self.charlie_gate,
                    config=strategy_cfg,
                    execution_service=self.execution,
                    kelly_sizer=self.kelly_sizer,
                    redis_subscriber=None,
                )
                logger.info("component_construct_success", component="latency_arb_strategy")
            else:
                logger.warning("strategy_disabled", strategy="latency_arb")

            # 13. Portfolio State — cached position/exposure snapshot for risk budget checks
            budget = GLOBAL_RISK_BUDGET
            logger.info("component_construct_begin", component="portfolio_state")
            self.portfolio_state = PortfolioState(
                ledger=self.ledger,
                equity=STARTING_CAPITAL,
                global_max_exposure_pct=float(budget.get("max_exposure_pct", Decimal("0.50"))),
                max_per_market_pct=float(budget.get("max_per_market_pct", Decimal("0.10"))),
            )
            await self._safe_await(
                "portfolio_state.refresh",
                self.portfolio_state.refresh(force=True),
            )
            logger.info("component_construct_success", component="portfolio_state")

            logger.info("all_components_initialized")
            
        except Exception as e:
            logger.error(
                "component_initialization_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise
    
    async def _check_database_health(self) -> bool:
        """Check database health."""
        try:
            if not self.ledger:
                return False
            
            # Simple query to verify connection
            equity = await self._safe_await("ledger.get_equity.health", self.ledger.get_equity(), default=None)
            return equity is not None
        except Exception:
            return False
    
    async def start(self):
        """Start the trading system."""
        logger.info("starting_trading_system")
        
        try:
            # Initialize all components
            await self._await_step("initialize_components", self.initialize_components(), timeout_seconds=120.0)

            # --- Startup reconciliation: recover any open orders from last run ---
            # Use the ledger's order_tracking table — single source of truth.
            reconcile_summary = await self._safe_await(
                "ledger.reconcile_open_orders",
                self.ledger.reconcile_open_orders(self.api_client),
                timeout_seconds=60.0,
                default={},
            )
            open_count = reconcile_summary.get("still_open", 0)
            resolved_count = reconcile_summary.get("resolved_while_offline", 0)
            recovered_pnl = reconcile_summary.get("recovered_pnl", Decimal("0"))
            print(
                f"\n=== STARTUP RECONCILIATION ===\n"
                f"  Recovered {open_count} open order(s) across markets.\n"
                f"  {resolved_count} order(s) resolved while offline.\n"
                f"  Recovered PnL: ${recovered_pnl:.2f} USDC\n"
                f"==============================\n"
            )
            logger.info(
                "startup_reconciliation_complete",
                open_orders=open_count,
                resolved_offline=resolved_count,
                recovered_pnl=str(recovered_pnl),
            )

            # Refresh performance tracker after reconcile
            if self.performance_tracker is not None:
                await self._safe_await(
                    "performance_tracker.refresh_post_reconcile",
                    self.performance_tracker.refresh(),
                )

            self.running = True

            logger.info(
                "trading_system_started",
                status="operational"
            )

            # Main loop
            await self._main_loop()
            
        except Exception as e:
            logger.error(
                "trading_system_start_failed",
                error=str(e),
                error_type=type(e).__name__
            )
            raise
    
    async def _main_loop(self):
        """Main trading loop."""
        logger.info("entering_main_loop", tick_seconds=self.loop_tick_seconds)
        
        iteration = 0
        
        while self.running:
            try:
                iteration += 1
                
                # Wait for shutdown or next iteration
                try:
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=self.loop_tick_seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Continue normal operation

                now = asyncio.get_event_loop().time()
                if (now - self.last_heartbeat_at) >= self.loop_tick_seconds:
                    self.last_heartbeat_at = now
                    logger.info(
                        "main_loop_heartbeat",
                        iteration=iteration,
                        uptime_seconds=round(now - self.start_time, 2),
                        ws_state=getattr(getattr(self.websocket, 'state', None), 'value', 'unknown')
                    )
                
                # Periodic tasks
                if iteration % 1 == 0:
                    await self._periodic_check()
                
                maintenance_every = max(1, int(60 / max(self.loop_tick_seconds, 1.0)))
                if iteration % maintenance_every == 0:
                    await self._periodic_maintenance()

                if (now - self.last_market_probe_at) >= self.market_probe_interval_seconds:
                    self.last_market_probe_at = now
                    await self._market_discovery_probe()

                await self._run_strategy_scan(trigger="main_loop")
                
            except Exception as e:
                logger.error(
                    "main_loop_error",
                    iteration=iteration,
                    error=str(e),
                    error_type=type(e).__name__
                )
                
                # Decide whether to continue
                if self.config.get('safety', {}).get('emergency_stop_on_error', True):
                    logger.critical("emergency_stop_triggered")
                    break
                
                # Otherwise, continue after delay
                await asyncio.sleep(10)
        
        logger.info("exiting_main_loop")
    
    async def _periodic_check(self):
        """Periodic health and status check."""
        try:
            # Check circuit breaker
            if self.circuit_breaker:
                equity = await self._safe_await("ledger.get_equity.periodic", self.ledger.get_equity(), default=Decimal('0'))
                can_trade = await self._safe_await(
                    "circuit_breaker.can_trade",
                    self.circuit_breaker.can_trade(equity),
                    default=False,
                )
                
                if not can_trade:
                    logger.warning(
                        "circuit_breaker_open",
                        status=self.circuit_breaker.get_status()
                    )
            
            # Log system status
            api_healthy = await self._safe_await(
                "api_client.health_check",
                self.api_client.health_check(),
                timeout_seconds=self.network_timeout_seconds,
                default=False,
            ) if self.api_client else False

            ws_healthy = await self._safe_await(
                "websocket.health_check",
                self.websocket.health_check(),
                timeout_seconds=self.network_timeout_seconds,
                default=False,
            ) if self.websocket else False

            latest_btc_price = await self._safe_await(
                "websocket.get_price.BTC",
                self.websocket.get_price("BTC"),
                timeout_seconds=3.0,
                default=None,
            ) if self.websocket else None

            if latest_btc_price is not None:
                logger.info("price_update", symbol="BTC", price=str(latest_btc_price), source="periodic_check")

            logger.info(
                "periodic_status_check",
                equity=float(equity) if self.ledger else 0,
                api_healthy=api_healthy,
                ws_connected=(getattr(getattr(self.websocket, 'state', None), 'value', '') == 'connected' if self.websocket else False),
                ws_healthy=ws_healthy,
                btc_price=str(latest_btc_price) if latest_btc_price is not None else None,
                circuit_breaker_state=self.circuit_breaker.state.value if self.circuit_breaker else 'unknown'
            )
            
            # Refresh performance tracker and enforce dynamic thresholds
            if self.performance_tracker is not None:
                await self._safe_await(
                    "performance_tracker.refresh",
                    self.performance_tracker.refresh(),
                    timeout_seconds=10.0,
                )
                summary = self.performance_tracker.get_summary()
                logger.info("performance_tracker_update", **{k: str(v) for k, v in summary.items()})

                max_dd_halt = PERFORMANCE_TRACKER_CONFIG.get("max_drawdown_halt", Decimal("0.15"))
                min_wr = PERFORMANCE_TRACKER_CONFIG.get("min_rolling_win_rate", Decimal("0.35"))
                win_rate_sample = int(PERFORMANCE_TRACKER_CONFIG.get("win_rate_min_sample", 20))

                current_dd = self.performance_tracker.get_current_drawdown()
                if current_dd >= Decimal(str(max_dd_halt)):
                    logger.critical(
                        "performance_halt_drawdown",
                        current_drawdown_pct=str(current_dd * 100),
                        threshold_pct=str(Decimal(str(max_dd_halt)) * 100),
                    )
                    if self.circuit_breaker:
                        from risk.circuit_breaker_v2 import TripReason
                        await self._safe_await(
                            "circuit_breaker.trip_drawdown",
                            self.circuit_breaker.trip(TripReason.MAX_DRAWDOWN),
                            default=None,
                        )

                rolling_wr = self.performance_tracker.get_rolling_win_rate(win_rate_sample)
                if rolling_wr is not None and Decimal(str(rolling_wr)) < Decimal(str(min_wr)):
                    logger.critical(
                        "performance_halt_win_rate",
                        rolling_win_rate=f"{rolling_wr:.2%}",
                        threshold=str(min_wr),
                        sample_size=win_rate_sample,
                    )
                    if self.circuit_breaker:
                        from risk.circuit_breaker_v2 import TripReason
                        await self._safe_await(
                            "circuit_breaker.trip_loss_streak",
                            self.circuit_breaker.trip(TripReason.LOSS_STREAK),
                            default=None,
                        )

        except Exception as e:
            logger.error(
                "periodic_check_failed",
                error=str(e)
            )
    
    async def _periodic_maintenance(self):
        """Periodic maintenance tasks."""
        try:
            # Validate ledger
            if self.ledger:
                is_balanced = await self._safe_await(
                    "ledger.validate_ledger",
                    self.ledger.validate_ledger(),
                    timeout_seconds=self.network_timeout_seconds,
                    default=True,
                )
                if not is_balanced:
                    logger.error("ledger_validation_failed", message="Ledger not balanced!")

            # -------------------------------------------------------------------
            # Online settlement: settle any open orders whose market resolved
            # while the bot is running.  Mirrors reconcile_open_orders (startup)
            # but runs on a live polling cadence so PnL is booked immediately
            # rather than waiting for next restart.
            # -------------------------------------------------------------------
            await self._safe_await(
                "settle_resolved_open_orders",
                self._settle_resolved_open_orders(),
                timeout_seconds=self.network_timeout_seconds,
                default=None,
            )

            # Clean up execution service
            if self.execution:
                cleaned = await self._safe_await(
                    "execution.cleanup_old_orders",
                    self.execution.cleanup_old_orders(max_age_seconds=3600),
                    timeout_seconds=self.network_timeout_seconds,
                    default=0,
                )
                if cleaned > 0:
                    logger.info("orders_cleaned_up", count=cleaned)
            
            # Clear cache
            if self.secrets_manager:
                self.secrets_manager.clear_cache()
            
        except Exception as e:
            logger.error(
                "periodic_maintenance_failed",
                error=str(e)
            )

    async def _settle_resolved_open_orders(self) -> None:
        """
        Online settlement path — runs every maintenance cycle (~60 s).

        Walks all open ``order_tracking`` rows and, for each market that has
        resolved (``closed=True`` or ``resolved=True`` on Polymarket), books
        realized PnL and transitions the row to ``SETTLED``.

        This is the live counterpart to ``AsyncLedger.reconcile_open_orders``
        (which only runs at startup).  Together they guarantee that settlement
        is booked at most two cycles after a market resolves, regardless of
        whether the bot was running at the exact moment of resolution.

        PnL formula (binary prediction market)
        ----------------------------------------
        quantity        = size_usdc / entry_price
        realized_pnl    = quantity * payout_per_share − size_usdc

        Where ``payout_per_share`` is either 1.0 (winner) or 0.0 (loser).
        This is the same formula used in ``reconcile_open_orders``.
        """
        if self.ledger is None or self.api_client is None:
            return

        open_orders = await self._safe_await(
            "ledger.get_open_orders.settlement_poll",
            self.ledger.get_open_orders(),
            timeout_seconds=10.0,
            default=[],
        )
        if not open_orders:
            return

        # De-duplicate market_id lookups so we don't hammer the API for every
        # order when multiple orders are open in the same market.
        market_cache: Dict[str, Any] = {}
        settled_count = 0

        for row in open_orders:
            order_id = row.get("order_id", "")
            market_id = row.get("market_id", "")
            if not order_id or not market_id:
                continue

            # Fetch (and cache) market metadata once per market per cycle.
            if market_id not in market_cache:
                market_data = await self._safe_await(
                    f"api_client.get_market.settlement_poll.{market_id}",
                    self.api_client.get_market(market_id),
                    timeout_seconds=8.0,
                    default=None,
                ) if hasattr(self.api_client, "get_market") else None
                market_cache[market_id] = market_data or {}

            market = market_cache[market_id]
            market_resolved = bool(market.get("closed") or market.get("resolved"))
            if not market_resolved:
                continue

            # Compute PnL using the same formula as reconcile_open_orders.
            raw_payout = market.get("payout_numerator") or market.get("payout_per_share")
            if raw_payout is None:
                # Market is closed but payout not yet posted — skip for now;
                # the next maintenance cycle will retry.
                logger.debug(
                    "settlement_poll_payout_pending",
                    order_id=order_id,
                    market_id=market_id,
                )
                continue

            try:
                size = Decimal(str(row.get("size", "0")))
                price = Decimal(str(row.get("price", "0")))
                payout_per_share = Decimal(str(raw_payout))
                quantity = size / price if price > Decimal("0") else Decimal("0")
                pnl = quantity * payout_per_share - size
            except Exception as exc:
                logger.warning(
                    "settlement_poll_pnl_compute_error",
                    order_id=order_id,
                    market_id=market_id,
                    error=str(exc),
                )
                continue

            winning_side = market.get("winning_side") or market.get("outcome")
            await self._safe_await(
                f"ledger.transition_order_state.settled_live.{order_id}",
                self.ledger.transition_order_state(
                    order_id,
                    "SETTLED",
                    pnl=pnl,
                    notes=(
                        f"resolved_live winning_side={winning_side} "
                        f"payout={payout_per_share}"
                    ),
                ),
                timeout_seconds=5.0,
            )
            settled_count += 1
            logger.info(
                "order_settled_live",
                order_id=order_id,
                market_id=market_id,
                pnl=str(pnl),
                winning_side=winning_side,
                payout_per_share=str(payout_per_share),
            )

        if settled_count > 0:
            logger.info(
                "settlement_poll_complete",
                settled=settled_count,
                checked=len(open_orders),
            )
    
    async def stop(self):
        """Stop the trading system."""
        logger.info("stopping_trading_system")
        
        self.running = False
        self.shutdown_event.set()
        
        try:
            # --- Shutdown snapshot: log final state before closing ---
            # Use ledger.shutdown_snapshot (order_tracking table) — single source of truth.
            if self.ledger is not None:
                price_feed = getattr(self, "api_client", None)
                snapshot = await self._safe_await(
                    "ledger.shutdown_snapshot",
                    self.ledger.shutdown_snapshot(price_feed=price_feed),
                    timeout_seconds=15.0,
                    default={},
                )
                print(
                    f"\n=== SHUTDOWN SNAPSHOT ===\n"
                    f"  Open positions     : {snapshot.get('open_positions', 'N/A')}\n"
                    f"  Total exposure     : ${snapshot.get('total_exposure_usdc', '?')} USDC\n"
                    f"  Mark-to-market PnL : ${snapshot.get('mark_to_market_pnl', '?')} USDC\n"
                    f"  Realized PnL (all) : ${snapshot.get('realized_pnl_all_time', '?')} USDC\n"
                    f"  Hit rate           : {snapshot.get('hit_rate', 'N/A')}\n"
                    f"  Markets            : {snapshot.get('markets', [])}\n"
                    f"========================\n"
                )

            # Close legacy order_store if still present (transition period)
            if self.order_store is not None:
                await self._safe_await(
                    "order_store.close", self.order_store.close(), timeout_seconds=5.0
                )
                logger.info("order_store_closed")

            # Stop health monitor
            if self.health_monitor:
                await self._safe_await("health_monitor.stop", self.health_monitor.stop(), timeout_seconds=15.0)
                logger.info("health_monitor_stopped")

            if self.execution:
                await self._safe_await("execution_service.stop", self.execution.stop(), timeout_seconds=15.0)
                logger.info("execution_service_stopped")
            
            # Stop WebSocket
            if self.websocket:
                await self._safe_await("websocket.stop", self.websocket.stop(), timeout_seconds=15.0)
                logger.info("websocket_stopped")
            
            # Close API client
            if self.api_client:
                await self._safe_await("api_client.close", self.api_client.close(), timeout_seconds=10.0)
                logger.info("api_client_closed")
            
            # Close ledger
            if self.ledger:
                await self._safe_await("ledger.close", self.ledger.close(), timeout_seconds=15.0)
                logger.info("ledger_closed")
            
            logger.info("trading_system_stopped")
            
        except Exception as e:
            logger.error(
                "shutdown_error",
                error=str(e),
                error_type=type(e).__name__
            )


async def main():
    """Main entry point."""
    # Parse arguments
    parser = argparse.ArgumentParser(description='Polymarket Trading Bot')
    parser.add_argument(
        '--config',
        default='config/production.yaml',
        help='Configuration file path'
    )
    parser.add_argument(
        '--mode',
        choices=['paper', 'live', 'replay'],
        default='paper',
        help='Trading mode.  "replay" re-evaluates history against current thresholds.'
    )
    parser.add_argument(
        '--replay-log',
        default='bot_production.log',
        help='Path to structlog JSON-lines log file for --mode replay.'
    )
    parser.add_argument(
        '--from', dest='from_ts',
        default=None,
        metavar='ISO8601',
        help='Replay start timestamp (UTC ISO-8601), e.g. 2026-01-01T00:00:00Z'
    )
    parser.add_argument(
        '--to', dest='to_ts',
        default=None,
        metavar='ISO8601',
        help='Replay end timestamp (UTC ISO-8601), e.g. 2026-02-01T00:00:00Z'
    )
    parser.add_argument(
        '--baseline',
        default='data/replay_baseline.json',
        help='Path to the regression-baseline JSON file read/written by --mode replay.'
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    # Configure logging
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Load configuration
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error("config_not_found", path=str(config_path))
        sys.exit(1)
    
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    # Override paper trading mode from command line
    if args.mode == 'replay':
        # Replay mode: re-run historical events from log through strategy + Kelly logic.
        # Never touches the exchange.
        from replay.engine import run_replay
        from datetime import datetime, timezone

        def _parse_iso(ts: str) -> datetime:
            """Accept ISO-8601 UTC strings with or without trailing Z."""
            ts = ts.rstrip('Z')
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

        from_ts = _parse_iso(args.from_ts) if args.from_ts else None
        to_ts = _parse_iso(args.to_ts) if args.to_ts else None
        await run_replay(
            log_file=args.replay_log,
            from_ts=from_ts,
            to_ts=to_ts,
            baseline_path=args.baseline,
        )
        return

    if 'trading' not in config:
        config['trading'] = {}
    config['trading']['paper_trading'] = (args.mode == 'paper')
    
    logger.info(
        "configuration_loaded",
        config_path=str(config_path),
        mode=args.mode,
        paper_trading=config['trading']['paper_trading']
    )
    
    # Create trading system
    system = TradingSystem(config)
    
    # Set up signal handlers
    def signal_handler(signum, frame):
        logger.info("shutdown_signal_received", signal=signum)
        asyncio.create_task(system.stop())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start system
    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt")
    except Exception as e:
        logger.error(
            "fatal_error",
            error=str(e),
            error_type=type(e).__name__
        )
        sys.exit(1)
    finally:
        await system.stop()


if __name__ == '__main__':
    asyncio.run(main())
