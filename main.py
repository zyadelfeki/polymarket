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
import inspect
import os
import signal
import sys
import argparse
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal
import yaml
import structlog
import config_production

from scripts.ensure_single_instance import acquire_instance_lock

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
from strategies.btc_price_level_scanner import BTCPriceLevelScanner
from utils.decimal_helpers import quantize_quantity, to_decimal, from_config

# Charlie integration — new modules
from risk.performance_tracker import PerformanceTracker
from risk.kelly_sizing import KellySizer
from integrations.charlie_booster import CharliePredictionGate, TradeRecommendation
from config_production import (
    CHARLIE_CONFIG,
    KELLY_CONFIG,
    PERFORMANCE_TRACKER_CONFIG,
    REGIME_RISK_OVERRIDES,
    GLOBAL_RISK_BUDGET,
    STARTING_CAPITAL,
    BLOCKED_MARKETS,
    MARKET_TAG_BLOCKLIST,
    REGIME_RISK_CONFIG,
)
from utils.market_performance_guard import is_market_blocked_by_performance
from services.portfolio_state import PortfolioState
from services.do_not_trade import DoNotTradeRegistry
from data_feeds.binance_features import get_all_features as _get_binance_features
from risk.portfolio_risk import PortfolioRiskEngine, DrawdownMonitor
from services.health_server import HealthServer
from services.calibration_observation_service import (
    CALIBRATION_DATASET_FIELDNAMES,
    CALIBRATION_OBSERVATION_FIELDNAMES,
    CalibrationObservationService,
)
from services.runtime_guard_evaluator import RuntimeGuardEvaluator
from database.quarantine_repository import QuarantineRepository

# Phase 3-6 features (log-only)
from data_feeds.arb_scanner import scan_yes_no_arb
from execution.sniper import MarketSniper
from execution.oracle_monitor import check_oracle_window
from data_feeds.binance_feed import BinanceTradeFeed

# Session 2: volatility / regime classifier
from utils.regime_features import get_regime_features as _get_regime_features
from utils.regime_classifier import (
    classify_regime as _classify_regime,
    get_current_regime as _get_current_regime,
    get_session_regime_stats as _get_regime_stats,
)

# Session 3: LLM market-tag blocklist
from utils.market_tags import is_market_blocked_by_tags as _is_market_blocked_by_tags

# Session 4: OFI execution policy (logging-only mode until offline-validated)
from execution.ofi_policy import (
    build_ofi_features as _build_ofi_features,
    log_ofi_action as _log_ofi_action,
    choose_execution_action as _ofi_choose_action,
)


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


def _resolve_runtime_controls(config: dict) -> dict:
    """Merge YAML runtime controls with safe defaults.

    `config_production.py` remains the default source for legacy constants, but
    live runtime behavior is driven from the parsed YAML config after this merge.
    """
    trading_cfg = config.setdefault("trading", {})
    runtime_controls = config.setdefault("runtime_controls", {})

    blocked_markets = runtime_controls.get("blocked_markets")
    if blocked_markets is None:
        blocked_markets = sorted(str(market_id) for market_id in BLOCKED_MARKETS)
    else:
        blocked_markets = [str(market_id) for market_id in blocked_markets]
    runtime_controls["blocked_markets"] = blocked_markets

    lifecycle_cfg = runtime_controls.get("lifecycle_guard") or {}
    runtime_controls["lifecycle_guard"] = {
        "enabled": bool(lifecycle_cfg.get("enabled", True)),
        "max_active_entries_per_market": int(
            lifecycle_cfg.get("max_active_entries_per_market", 1)
        ),
        "allow_add_on": bool(lifecycle_cfg.get("allow_add_on", False)),
        "min_price_improvement_abs": str(
            lifecycle_cfg.get("min_price_improvement_abs", "0.05")
        ),
    }

    max_entry_price_abs = runtime_controls.get(
        "max_entry_price_abs",
        trading_cfg.get("max_entry_price_abs", "0.65"),
    )
    trading_cfg["max_entry_price_abs"] = str(max_entry_price_abs)
    runtime_controls["max_entry_price_abs"] = str(max_entry_price_abs)

    calibration_cfg = runtime_controls.get("calibration") or {}
    runtime_controls["calibration"] = {
        "fail_closed": bool(calibration_cfg.get("fail_closed", True)),
        "min_positive_coef": float(calibration_cfg.get("min_positive_coef", 0.0)),
        "require_monotonic_smoke_test": bool(
            calibration_cfg.get("require_monotonic_smoke_test", True)
        ),
        "smoke_test_points": list(
            calibration_cfg.get("smoke_test_points", [0.30, 0.50, 0.79])
        ),
        "observe_only_on_invalid": bool(
            calibration_cfg.get("observe_only_on_invalid", True)
        ),
        "dataset_export_path": str(
            calibration_cfg.get(
                "dataset_export_path",
                calibration_cfg.get("dataset_path", "data/calibration_dataset_v2.csv"),
            )
        ),
        "observation_export_path": str(
            calibration_cfg.get(
                "observation_export_path",
                calibration_cfg.get("observation_store_path", "data/calibration_observations.csv"),
            )
        ),
        "meta_candidate_feature_schema_version": str(
            calibration_cfg.get("meta_candidate_feature_schema_version", "meta_candidate_v1")
        ),
        "meta_candidate_cluster_policy_version": str(
            calibration_cfg.get("meta_candidate_cluster_policy_version", "cluster_v1")
        ),
        "meta_candidate_cluster_time_bucket_seconds": int(
            calibration_cfg.get("meta_candidate_cluster_time_bucket_seconds", 10)
        ),
        "meta_candidate_cluster_price_bucket_abs": str(
            calibration_cfg.get("meta_candidate_cluster_price_bucket_abs", "0.01")
        ),
    }

    meta_gate_cfg = runtime_controls.get("meta_gate") or {}
    runtime_controls["meta_gate"] = {
        "decision_mode": "shadow",
        "shadow_only": True,
        "feature_schema_version": str(
            meta_gate_cfg.get(
                "feature_schema_version",
                runtime_controls["calibration"]["meta_candidate_feature_schema_version"],
            )
        ),
        "calibration_version": str(
            meta_gate_cfg.get("calibration_version", "platt_scaler_v1")
        ),
    }

    quarantine_cfg = runtime_controls.get("quarantine") or {}
    runtime_controls["quarantine"] = {
        "enabled": bool(quarantine_cfg.get("enabled", True)),
        "seed_static_blocklist": bool(
            quarantine_cfg.get("seed_static_blocklist", True)
        ),
        "auto_review_after_days": int(
            quarantine_cfg.get("auto_review_after_days", 7)
        ),
    }

    runtime_controls["session_snapshot_interval_seconds"] = int(
        runtime_controls.get("session_snapshot_interval_seconds", 60)
    )
    return config


async def _get_rolling_features(
    ledger,
    n_win: int = 20,
    n_pnl: int = 10,
) -> tuple:
    """
    Query the last max(n_win, n_pnl) settled PnLs from order_tracking and
    return (rolling_win_rate, rolling_pnl_z) for the meta-gate feature vector.

    rolling_win_rate: fraction of the last n_win settled trades that were wins.
    rolling_pnl_z: z-score of the most recent PnL vs the preceding n_pnl-1 PnLs
                   (peers = all-but-most-recent so no self-contamination).

    Returns (0.5, 0.0) if the DB call fails or there is insufficient history.
    """
    try:
        rows = await ledger.execute(
            "SELECT pnl FROM order_tracking "
            "WHERE order_state='SETTLED' AND pnl IS NOT NULL AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?",
            (max(n_win, n_pnl),),
            fetch_all=True,
            as_dict=True,
        )
    except TypeError as _e:
        # ledger.execute() doesn't support as_dict= on this version; feature
        # will be neutral-defaulted until the ledger is updated.
        logger.warning(
            "rolling_features_as_dict_unsupported",
            error=str(_e),
            fallback="0.5/0.0",
            hint="AsyncLedger.execute must accept as_dict=True for rolling meta-gate features",
        )
        return 0.5, 0.0
    if not rows:
        return 0.5, 0.0

    pnls = [float(r["pnl"] if isinstance(r, dict) else r[0]) for r in rows]

    # rolling_win_rate: require a minimum sample count before trusting the rate.
    # Below WIN_MIN_SAMPLE trades the estimate is too noisy — hold at 0.5 neutral
    # so it doesn't bias the meta-gate with an extreme 0.0 or 1.0 prior.
    WIN_MIN_SAMPLE = 5
    win_slice = pnls[:n_win]
    rolling_win_rate = (
        sum(1 for p in win_slice if p > 0) / len(win_slice)
        if len(win_slice) >= WIN_MIN_SAMPLE
        else 0.5  # insufficient history — stay neutral
    )

    # rolling_pnl_z: most recent PnL vs the preceding n_pnl-1 peers (no self-contamination)
    pnl_slice = pnls[:n_pnl]
    if len(pnl_slice) >= 2:
        peers = pnl_slice[1:]  # exclude most recent (index 0 = newest in DESC order)
        mu = sum(peers) / len(peers)
        variance = sum((x - mu) ** 2 for x in peers) / len(peers)
        sigma = variance ** 0.5 + 1e-9
        rolling_pnl_z = (pnl_slice[0] - mu) / sigma
    else:
        rolling_pnl_z = 0.0

    return rolling_win_rate, rolling_pnl_z


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
        self.runtime_controls = config.get("runtime_controls", {})
        self.blocked_markets = {
            str(market_id) for market_id in self.runtime_controls.get("blocked_markets", [])
        }
        self.lifecycle_guard_config = self.runtime_controls.get("lifecycle_guard", {})
        self.calibration_guard_config = self.runtime_controls.get("calibration", {})
        self.quarantine_config = self.runtime_controls.get("quarantine", {})
        self.calibration_dataset_path = Path(
            str(self.calibration_guard_config.get("dataset_export_path", "data/calibration_dataset_v2.csv"))
        )
        self.calibration_observations_path = Path(
            str(self.calibration_guard_config.get("observation_export_path", "data/calibration_observations.csv"))
        )
        self._market_quarantine: Dict[str, Dict[str, Any]] = {}
        self.guard_evaluator = RuntimeGuardEvaluator(
            lifecycle_guard_config=self.lifecycle_guard_config,
            calibration_guard_config=self.calibration_guard_config,
        )
        self.quarantine_repository: Optional[QuarantineRepository] = None
        self.calibration_observation_service: Optional[CalibrationObservationService] = None
        
        # Components (initialized in start())
        self.secrets_manager: Optional[SecretsManager] = None
        self.ledger: Optional[AsyncLedger] = None
        self.api_client: Optional[PolymarketClientV2] = None
        self.websocket: Optional[BinanceWebSocketV2] = None
        self.execution: Optional[ExecutionServiceV2] = None
        self.health_monitor: Optional[HealthMonitorV2] = None
        self.circuit_breaker: Optional[CircuitBreakerV2] = None
        self.strategy_engine: Optional[MultiTimeframeLatencyArbitrageEngine] = None
        self.btc_price_scanner: Optional[BTCPriceLevelScanner] = None
        self.strategy_scan_lock = asyncio.Lock()
        self.last_strategy_scan_at = 0.0

        # Charlie integration components
        self.order_store = None  # Removed — order tracking lives in ledger.order_tracking table
        self.performance_tracker: Optional[PerformanceTracker] = None
        self.kelly_sizer: Optional[KellySizer] = None
        self.charlie_gate: Optional[CharliePredictionGate] = None
        self.portfolio_state: Optional[PortfolioState] = None
        self.portfolio_risk_engine: Optional[PortfolioRiskEngine] = None
        self.drawdown_monitor: Optional[DrawdownMonitor] = None
        self.health_server: Optional[HealthServer] = None
        self.do_not_trade: DoNotTradeRegistry = DoNotTradeRegistry(
            path="data/do_not_trade.json",
            auto_load=True,
        )
        # Tracks the last submission time per (market_id, token_id) pair for
        # paper mode.  Prevents duplicate orders when rapid price ticks fire
        # multiple scans before the idempotency cache would dedup them (the
        # cache key includes price, so a 1-tick price change defeats it).
        self._paper_order_cooldowns: Dict[str, float] = {}
        # Per-market lifecycle state, populated from actual submitted/open orders.
        # This is the authoritative market-entry guard used to prevent repeated
        # entries, side flips, and uncontrolled add-ons.
        self._market_lifecycle_state: Dict[str, Dict[str, Any]] = {}
        # Session-level event counters for the end-of-session observability report.
        # Incremented at each decision point; logged on stop().
        self._session_stats: Dict[str, int] = {
            "opportunities_evaluated": 0,
            "blocked_static_list": 0,
            "blocked_quarantine": 0,
            "blocked_lifecycle_guard": 0,
            "blocked_side_flip_rule": 0,
            "blocked_charlie_rejected": 0,
            "blocked_meta_gate": 0,
            "blocked_risk_budget": 0,
            "blocked_max_entry_price": 0,
            "blocked_bad_calibration": 0,
            "observe_only_bad_calibration": 0,
            "orders_submitted": 0,
        }
        self._calibration_guard_status: Dict[str, Any] = {
            "blocked": False,
            "reason": None,
            "coef": None,
            "monotonic": None,
            "smoke_results": [],
        }
        self._last_session_snapshot_at: float = 0.0
        self._paper_session_start: Optional[str] = None
        self.last_discovered_markets = []
        self.market_sniper = MarketSniper()
        self.binance_trade_feed: Optional[BinanceTradeFeed] = None
        startup_config = config.get('startup', {})

        # Session 2: dynamic regime state — updated every ~60 s by
        # _periodic_maintenance.  Used to scale Kelly size per-regime without
        # rewriting config files or reinitialising the KellySizer.
        # None = classifier not yet initialised; first maintenance tick sets it.
        self._dynamic_regime: Optional[str] = None
        self._dynamic_regime_ts: float = 0.0   # monotonic; gate against rapid updates
        self.init_timeout_seconds = float(startup_config.get('component_timeout_seconds', 25.0))
        self.network_timeout_seconds = float(startup_config.get('network_timeout_seconds', 20.0))
        self.loop_tick_seconds = float(startup_config.get('loop_tick_seconds', 10.0))
        self.market_probe_interval_seconds = float(startup_config.get('market_probe_interval_seconds', 30.0))
        self.market_probe_limit = int(startup_config.get('market_probe_limit', 10))
        self.strategy_scan_min_interval_seconds = float(startup_config.get('strategy_scan_min_interval_seconds', 2.0))
        self.strategy_scan_timeout_seconds = float(startup_config.get('strategy_scan_timeout_seconds', 30.0))
        self.last_market_probe_at = 0.0
        self.last_heartbeat_at = 0.0
        self.start_time = time.monotonic()  # monotonic wall clock; asyncio loop.time() is the same source
        
        logger.info(
            "trading_system_initialized",
            environment=config.get('environment', 'unknown'),
            paper_trading=config.get('trading', {}).get('paper_trading', True),
            init_timeout_seconds=self.init_timeout_seconds,
            network_timeout_seconds=self.network_timeout_seconds,
            loop_tick_seconds=self.loop_tick_seconds,
            strategy_scan_min_interval_seconds=self.strategy_scan_min_interval_seconds,
            blocked_markets_count=len(self.blocked_markets),
            active_quarantines=len(self._market_quarantine),
            lifecycle_guard=self.lifecycle_guard_config,
            calibration_guard=self.calibration_guard_config,
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
        timeout = float(timeout_seconds if timeout_seconds is not None else getattr(self, "network_timeout_seconds", 20.0))
        try:
            if not inspect.isawaitable(coro):
                return coro
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

    def _compat_api_client(self):
        return getattr(self, "api_client", None) or getattr(self, "polymarket_client", None)

    def _compat_execution_service(self):
        return getattr(self, "execution_service", None) or getattr(self, "execution", None)

    def _ensure_runtime_services(self) -> None:
        if not isinstance(self.ledger, AsyncLedger):
            return
        if self.quarantine_repository is None:
            self.quarantine_repository = QuarantineRepository(self.ledger)
        if self.calibration_observation_service is None:
            self.calibration_observation_service = CalibrationObservationService(
                ledger=self.ledger,
                observation_export_path=str(self.calibration_observations_path),
                dataset_export_path=str(self.calibration_dataset_path),
                feature_schema_version=str(self.calibration_guard_config.get("meta_candidate_feature_schema_version", "meta_candidate_v1")),
                cluster_policy_version=str(self.calibration_guard_config.get("meta_candidate_cluster_policy_version", "cluster_v1")),
                cluster_time_bucket_seconds=int(self.calibration_guard_config.get("meta_candidate_cluster_time_bucket_seconds", 10)),
                cluster_price_bucket_abs=str(self.calibration_guard_config.get("meta_candidate_cluster_price_bucket_abs", "0.01")),
            )

    def _evaluate_calibration_guard(self) -> Dict[str, Any]:
        return self.guard_evaluator.evaluate_calibration_guard()

    def _check_lifecycle_guard(
        self,
        *,
        market_id: str,
        side: str,
        token_price: Decimal,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        return self.guard_evaluator.check_lifecycle_guard(
            self._market_lifecycle_state,
            market_id=market_id,
            side=side,
            token_price=token_price,
        )

    def _record_lifecycle_entry(
        self,
        *,
        market_id: str,
        side: str,
        token_id: str,
        token_price: Decimal,
        order_id: Optional[str] = None,
    ) -> None:
        state = self._market_lifecycle_state.get(market_id)
        if state is None:
            self._market_lifecycle_state[market_id] = {
                "side": side,
                "token_id": token_id,
                "entry_count": 1,
                "best_token_price": str(token_price),
                "last_token_price": str(token_price),
                "last_order_id": order_id,
                "opened_at_monotonic": asyncio.get_running_loop().time(),
            }
            return

        best_price = min(Decimal(str(state.get("best_token_price") or token_price)), token_price)
        state.update(
            {
                "side": side,
                "token_id": token_id,
                "entry_count": int(state.get("entry_count") or 0) + 1,
                "best_token_price": str(best_price),
                "last_token_price": str(token_price),
                "last_order_id": order_id,
                "updated_at_monotonic": asyncio.get_running_loop().time(),
            }
        )

    def _clear_lifecycle_entry(self, market_id: str, *, reason: str) -> None:
        if market_id in self._market_lifecycle_state:
            self._market_lifecycle_state.pop(market_id, None)
            logger.info("lifecycle_state_cleared", market_id=market_id, reason=reason)

    async def _sync_lifecycle_state_from_open_orders(self) -> None:
        if self.ledger is None:
            return
        open_orders = await self._safe_await(
            "ledger.get_open_orders.lifecycle_sync",
            self.ledger.get_open_orders(),
            default=[],
        )
        synced_markets: Dict[str, Dict[str, Any]] = {}
        for row in open_orders or []:
            market_id = str(row.get("market_id") or "")
            if not market_id:
                continue
            state = synced_markets.setdefault(
                market_id,
                {
                    "side": str(row.get("outcome") or ""),
                    "token_id": str(row.get("token_id") or ""),
                    "entry_count": 0,
                    "best_token_price": str(row.get("price") or "0"),
                    "last_token_price": str(row.get("price") or "0"),
                    "last_order_id": row.get("order_id"),
                    "opened_at_monotonic": asyncio.get_running_loop().time(),
                },
            )
            state["entry_count"] += 1
            price = Decimal(str(row.get("price") or "0"))
            best_price = Decimal(str(state.get("best_token_price") or price))
            if state["entry_count"] == 1 or price < best_price:
                state["best_token_price"] = str(price)
            state["last_token_price"] = str(price)
            state["last_order_id"] = row.get("order_id")

        self._market_lifecycle_state = synced_markets
        logger.info(
            "lifecycle_state_synced",
            markets=len(self._market_lifecycle_state),
            market_ids=sorted(self._market_lifecycle_state.keys()),
        )

    async def _reconcile_missing_open_orders_on_startup(self) -> Dict[str, int]:
        if self.ledger is None or self.api_client is None:
            return {
                "exchange_open_orders": 0,
                "imported": 0,
                "already_known": 0,
                "skipped": 0,
                "cancelled": 0,
                "cancel_failures": 0,
                "left_open_after_failed_cancel": 0,
            }

        exchange_open_orders = await self._safe_await(
            "api_client.get_open_orders.startup_reconcile",
            self.api_client.get_open_orders(),
            timeout_seconds=30.0,
            default=[],
        ) or []
        local_open_orders = await self._safe_await(
            "ledger.get_open_orders.startup_reconcile",
            self.ledger.get_open_orders(),
            timeout_seconds=30.0,
            default=[],
        ) or []

        known_order_ids = {
            str(order.get("order_id") or "")
            for order in local_open_orders
            if str(order.get("order_id") or "")
        }

        imported = 0
        already_known = 0
        skipped = 0
        cancelled = 0
        cancel_failures = 0
        left_open_after_failed_cancel = 0

        for order in exchange_open_orders:
            order_id = str(order.get("order_id") or "")
            market_id = str(order.get("market_id") or "")
            if not order_id or not market_id:
                skipped += 1
                continue

            if order_id in known_order_ids:
                already_known += 1
                continue

            size = Decimal(str(order.get("size") or "0"))
            price = Decimal(str(order.get("price") or "0"))
            if size <= 0 or price <= 0:
                skipped += 1
                continue

            await self.ledger.import_exchange_open_order(
                order_id=order_id,
                market_id=market_id,
                token_id=str(order.get("token_id") or ""),
                outcome=str(order.get("outcome") or order.get("side") or "UNKNOWN"),
                side=str(order.get("side") or "BUY"),
                size=size,
                price=price,
                opened_at=order.get("opened_at"),
                notes="startup_open_order_reconcile",
            )
            known_order_ids.add(order_id)
            imported += 1

            cancel_error: Optional[str] = None
            cancel_ok = False
            if hasattr(self.api_client, "cancel_order"):
                try:
                    cancel_ok = await asyncio.wait_for(
                        self.api_client.cancel_order(order_id), timeout=8.0
                    )
                except Exception as exc:
                    cancel_error = str(exc)
            else:
                cancel_error = "cancel_order_not_available"

            if cancel_ok:
                await self.ledger.transition_order_state(
                    order_id,
                    "CANCELLED",
                    notes="startup_orphan_auto_cancelled",
                )
                cancelled += 1
                logger.info(
                    "startup_orphan_order_auto_cancelled",
                    order_id=order_id,
                    market_id=market_id,
                )
            else:
                cancel_failures += 1
                left_open_after_failed_cancel += 1
                logger.warning(
                    "startup_orphan_order_auto_cancel_failed",
                    order_id=order_id,
                    market_id=market_id,
                    error=cancel_error or "cancel_returned_false",
                )

        logger.info(
            "startup_open_order_reconciliation_complete",
            exchange_open_orders=len(exchange_open_orders),
            imported=imported,
            already_known=already_known,
            skipped=skipped,
            cancelled=cancelled,
            cancel_failures=cancel_failures,
            left_open_after_failed_cancel=left_open_after_failed_cancel,
        )
        return {
            "exchange_open_orders": len(exchange_open_orders),
            "imported": imported,
            "already_known": already_known,
            "skipped": skipped,
            "cancelled": cancelled,
            "cancel_failures": cancel_failures,
            "left_open_after_failed_cancel": left_open_after_failed_cancel,
        }

    async def _reconcile_positions_on_startup(self) -> Dict[str, int]:
        api_client = self._compat_api_client()
        if self.ledger is None or api_client is None:
            return {"exchange_positions": 0, "imported": 0, "already_known": 0, "skipped": 0}

        exchange_positions = await self._safe_await(
            "api_client.get_open_positions.startup_reconcile",
            api_client.get_open_positions(),
            timeout_seconds=30.0,
            default=[],
        ) or []
        local_positions = await self._safe_await(
            "ledger.get_open_positions.startup_reconcile",
            self.ledger.get_open_positions(),
            timeout_seconds=30.0,
            default=[],
        ) or []

        known_keys = {
            (
                str((position.get("market_id") if isinstance(position, dict) else getattr(position, "market_id", "")) or ""),
                str((position.get("token_id") if isinstance(position, dict) else getattr(position, "token_id", "")) or ""),
            )
            for position in local_positions
        }

        imported = 0
        already_known = 0
        skipped = 0
        record_position = getattr(self.ledger, "record_reconciled_position", None)
        if record_position is None:
            record_position = getattr(self.ledger, "record_trade_entry", None)

        for position in exchange_positions:
            market_id = str(position.get("market_id") or "")
            token_id = str(position.get("token_id") or "")
            if not market_id or not token_id:
                skipped += 1
                continue

            position_key = (market_id, token_id)
            if position_key in known_keys:
                already_known += 1
                continue

            quantity_raw = position.get("quantity") or position.get("size") or position.get("shares") or "0"
            price_raw = position.get("price") or position.get("entry_price") or position.get("avg_price") or "0"

            quantity = Decimal(str(quantity_raw))
            entry_price = Decimal(str(price_raw))
            side = str(position.get("side") or position.get("outcome") or "UNKNOWN")

            if quantity <= 0 or entry_price <= 0 or record_position is None:
                skipped += 1
                continue

            await self._safe_await(
                "ledger.record_reconciled_position.startup_reconcile",
                record_position(
                market_id=market_id,
                token_id=token_id,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                metadata={"source": "startup_position_reconcile", "exchange_position": dict(position)},
                ),
                timeout_seconds=10.0,
            )
            known_keys.add(position_key)
            imported += 1

        logger.info(
            "startup_position_reconciliation_complete",
            exchange_positions=len(exchange_positions),
            imported=imported,
            already_known=already_known,
            skipped=skipped,
        )
        return {
            "exchange_positions": len(exchange_positions),
            "imported": imported,
            "already_known": already_known,
            "skipped": skipped,
        }

    async def _handle_resolved_position(self, position) -> None:
        ledger = getattr(self, "ledger", None)
        execution_service = self._compat_execution_service()
        market_id = str(position.get("market_id") if isinstance(position, dict) else getattr(position, "market_id", ""))
        token_id = str(position.get("token_id") if isinstance(position, dict) else getattr(position, "token_id", ""))
        strategy = str(position.get("strategy") if isinstance(position, dict) else getattr(position, "strategy", ""))
        quantity = Decimal(str(position.get("quantity") if isinstance(position, dict) else getattr(position, "quantity", "0")))
        entry_price = Decimal(str(position.get("entry_price") if isinstance(position, dict) else getattr(position, "entry_price", "0")))

        result = None
        if execution_service is not None and hasattr(execution_service, "close_position"):
            result = await self._safe_await(
                f"execution.close_position.market_resolution.{market_id}",
                execution_service.close_position(
                    market_id=market_id,
                    token_id=token_id,
                    strategy=strategy,
                    quantity=quantity,
                ),
                timeout_seconds=10.0,
                default=None,
            )

        filled_price = entry_price
        if result is not None:
            filled_price = Decimal(str(getattr(result, "filled_price", entry_price)))

        if ledger is not None and hasattr(ledger, "record_trade_exit"):
            await self._safe_await(
                f"ledger.record_trade_exit.market_resolution.{market_id}",
                ledger.record_trade_exit(
                    position_id=position.get("id") if isinstance(position, dict) else getattr(position, "id", None),
                    market_id=market_id,
                    token_id=token_id,
                    strategy=strategy,
                    exit_price=filled_price,
                    quantity=quantity,
                    metadata={"source": "market_resolution_monitor"},
                ),
                timeout_seconds=10.0,
                default=None,
            )

    async def _market_resolution_monitor(self) -> None:
        api_client = self._compat_api_client()
        ledger = getattr(self, "ledger", None)
        config = getattr(self, "config", {}) or {}
        interval_seconds = float(config.get("market_monitor_interval", 5.0))

        while getattr(self, "running", False):
            if ledger is None or api_client is None:
                await asyncio.sleep(interval_seconds)
                continue

            open_positions = await self._safe_await(
                "ledger.get_open_positions.market_resolution",
                ledger.get_open_positions(),
                timeout_seconds=10.0,
                default=[],
            ) or []

            for position in open_positions:
                market_id = str(position.get("market_id") if isinstance(position, dict) else getattr(position, "market_id", ""))
                if not market_id or not hasattr(api_client, "get_market"):
                    continue

                market = await self._safe_await(
                    f"api_client.get_market.market_resolution.{market_id}",
                    api_client.get_market(market_id),
                    timeout_seconds=8.0,
                    default=None,
                ) or {}

                status = str(market.get("status") or "").upper()
                is_resolved = status in {"RESOLVED", "CLOSED"} or bool(market.get("resolved") or market.get("closed"))
                if is_resolved:
                    await self._handle_resolved_position(position)
                    if not getattr(self, "running", False):
                        return

            if not getattr(self, "running", False):
                return
            await asyncio.sleep(interval_seconds)

    def _log_session_snapshot(self, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        min_interval = float(self.runtime_controls.get("session_snapshot_interval_seconds", 60))
        if not force and (now - self._last_session_snapshot_at) < min_interval:
            return
        self._last_session_snapshot_at = now
        logger.info(
            "session_snapshot",
            opportunities_evaluated=self._session_stats.get("opportunities_evaluated", 0),
            orders_submitted=self._session_stats.get("orders_submitted", 0),
            blocked_by_blocklist=self._session_stats.get("blocked_static_list", 0),
            blocked_by_quarantine=self._session_stats.get("blocked_quarantine", 0),
            blocked_by_lifecycle_guard=self._session_stats.get("blocked_lifecycle_guard", 0),
            blocked_by_side_flip_rule=self._session_stats.get("blocked_side_flip_rule", 0),
            blocked_by_max_entry_price=self._session_stats.get("blocked_max_entry_price", 0),
            blocked_by_bad_calibration=self._session_stats.get("blocked_bad_calibration", 0),
            observe_only_bad_calibration=self._session_stats.get("observe_only_bad_calibration", 0),
            blocked_by_meta_gate=self._session_stats.get("blocked_meta_gate", 0),
            blocked_by_risk_budget=self._session_stats.get("blocked_risk_budget", 0),
            lifecycle_markets=len(self._market_lifecycle_state),
            calibration_blocked=self._calibration_guard_status.get("blocked", False),
            calibration_reason=self._calibration_guard_status.get("reason"),
        )

    @staticmethod
    def _parse_note_token(notes: str, field: str) -> Optional[str]:
        prefix = f"{field}="
        for part in (notes or "").split():
            if part.startswith(prefix):
                return part[len(prefix):]
        return None

    async def _initialize_quarantine_store(self) -> None:
        self._ensure_runtime_services()
        if (
            self.ledger is None
            or self.quarantine_repository is None
            or not self.quarantine_config.get("enabled", True)
        ):
            return

        self._market_quarantine = await self._safe_await(
            "quarantine_repository.load_active_entries",
            self.quarantine_repository.load_active_entries(),
            default={},
        ) or {}

        await self._seed_static_quarantine()
        logger.info(
            "quarantine_store_initialized",
            backend="sqlite",
            active_quarantines=len(self._market_quarantine),
            auto_review_after_days=int(self.quarantine_config.get("auto_review_after_days", 7)),
        )

    async def _seed_static_quarantine(self) -> None:
        if self.quarantine_repository is None or not self.quarantine_config.get("enabled", True):
            return
        if not self.quarantine_config.get("seed_static_blocklist", True):
            return

        changed = await self._safe_await(
            "quarantine_repository.seed_runtime_blocklist",
            self.quarantine_repository.seed_runtime_blocklist(
                self.blocked_markets,
                auto_review_after_days=int(self.quarantine_config.get("auto_review_after_days", 7)),
            ),
            default=0,
        ) or 0
        self._market_quarantine = await self._safe_await(
            "quarantine_repository.reload_after_seed",
            self.quarantine_repository.load_active_entries(),
            default=self._market_quarantine,
        ) or self._market_quarantine

        if changed:
            logger.info(
                "quarantine_seeded_from_runtime_controls",
                added=changed,
                total=len(self._market_quarantine),
            )

    def _get_active_quarantine_entry(self, market_id: str) -> Optional[Dict[str, Any]]:
        if self.quarantine_repository is None or not self.quarantine_config.get("enabled", True):
            return None
        return self.quarantine_repository.get_active_entry(self._market_quarantine, market_id)

    async def _update_calibration_observation(self, observation_id: Optional[str], **updates: Any) -> None:
        self._ensure_runtime_services()
        if self.calibration_observation_service is None:
            return
        await self.calibration_observation_service.update_observation(observation_id, **updates)

    async def _record_calibration_observation(
        self,
        *,
        market_id: str,
        token_id: str,
        opportunity: Dict[str, Any],
        charlie_rec: TradeRecommendation,
        token_price: Decimal,
        normalized_yes_price: Decimal,
        trigger: str,
        observation_mode: str,
        guard_block_reason: str = "",
    ) -> str:
        self._ensure_runtime_services()
        if self.calibration_observation_service is None:
            return ""
        return await self.calibration_observation_service.record_observation(
            market_id=market_id,
            token_id=token_id,
            opportunity=opportunity,
            charlie_rec=charlie_rec,
            token_price=token_price,
            normalized_yes_price=normalized_yes_price,
            trigger=trigger,
            observation_mode=observation_mode,
            calibration_blocked=bool(self._calibration_guard_status.get("blocked")),
            guard_block_reason=guard_block_reason,
        )

    async def _resolve_pending_calibration_observations(self) -> None:
        self._ensure_runtime_services()
        if self.calibration_observation_service is None:
            return
        resolved_count = await self.calibration_observation_service.resolve_pending_observations(
            self.api_client,
            self._safe_await,
        )
        if resolved_count:
            logger.info(
                "calibration_observations_resolved",
                resolved_count=resolved_count,
                dataset_export_path=str(self.calibration_dataset_path),
            )

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

    # Supported symbols that should trigger a strategy scan when price updates.
    _STRATEGY_TRIGGER_SYMBOLS = {"BTC", "ETH", "SOL", "XRP"}

    async def _on_price_update(self, symbol: str, price_data) -> None:
        try:
            if symbol not in self._STRATEGY_TRIGGER_SYMBOLS:
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
        if not self.strategy_engine and not self.btc_price_scanner:
            return

        now = asyncio.get_running_loop().time()
        if (now - self.last_strategy_scan_at) < self.strategy_scan_min_interval_seconds:
            return

        if self.strategy_scan_lock.locked():
            return

        async with self.strategy_scan_lock:
            self.last_strategy_scan_at = now
            if self.health_server is not None:
                self.health_server.record_scan()
            logger.info("strategy_scan_begin", trigger=trigger)

            candidates: List[Dict[str, Any]] = []

            if self.strategy_engine:
                try:
                    opportunity = await asyncio.wait_for(
                        self.strategy_engine.scan_opportunities(),
                        timeout=self.strategy_scan_timeout_seconds,
                    )
                    if opportunity:
                        candidates.append(opportunity)
                except asyncio.TimeoutError:
                    logger.warning(
                        "strategy_scan_timeout",
                        trigger=trigger,
                        strategy="latency_arb",
                        timeout_seconds=self.strategy_scan_timeout_seconds,
                    )
                except Exception as e:
                    logger.error(
                        "strategy_scan_failed",
                        trigger=trigger,
                        strategy="latency_arb",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

            if self.btc_price_scanner and self.charlie_gate and self.api_client and self.ledger:
                try:
                    equity = await self.ledger.get_equity()
                    scanner_opportunities = await asyncio.wait_for(
                        self.btc_price_scanner.scan(
                            charlie_gate=self.charlie_gate,
                            api_client=self.api_client,
                            equity=to_decimal(equity),
                        ),
                        timeout=self.strategy_scan_timeout_seconds,
                    )
                    if scanner_opportunities:
                        candidates.extend(scanner_opportunities)
                except asyncio.TimeoutError:
                    logger.warning(
                        "strategy_scan_timeout",
                        trigger=trigger,
                        strategy="btc_price_scanner",
                        timeout_seconds=self.strategy_scan_timeout_seconds,
                    )
                except Exception as e:
                    logger.error(
                        "strategy_scan_failed",
                        trigger=trigger,
                        strategy="btc_price_scanner",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

            opportunity = None
            if candidates:
                opportunity = max(
                    candidates,
                    key=lambda item: to_decimal(item.get("edge", "0")),
                )
                logger.info(
                    "strategy_scan_candidates_summary",
                    trigger=trigger,
                    total_candidates=len(candidates),
                )
            else:
                logger.info(
                    "strategy_scan_zero_candidates",
                    trigger=trigger,
                    latency_arb_ran=(self.strategy_engine is not None),
                    btc_scanner_ran=(self.btc_price_scanner is not None and self.charlie_gate is not None),
                )

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
                asset=opportunity.get("asset"),
                question=(opportunity.get("question") or "")[:80],
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
        self._session_stats["opportunities_evaluated"] += 1
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

        calibration_blocked = bool(self._calibration_guard_status.get("blocked"))
        observe_only_bad_calibration = calibration_blocked and bool(
            self.calibration_guard_config.get("observe_only_on_invalid", True)
        )

        # --- Dynamic performance guard (auto-blocks bad markets after 5+ trades) ---
        if is_market_blocked_by_performance(market_id):
            logger.info(
                "market_blocked",
                reason="performance_guard_auto_block",
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

        # --- Session 3: LLM tag-based blocklist ---
        # Fail-open: _is_market_blocked_by_tags returns False on any error.
        _mkt_question_for_tag = str(opportunity.get("question") or "")
        if _is_market_blocked_by_tags(market_id, _mkt_question_for_tag, MARKET_TAG_BLOCKLIST):
            logger.info(
                "market_blocked_tag",
                reason="tag_blocklist_match",
                market_id=market_id,
                question=_mkt_question_for_tag[:80],
                trigger=trigger,
            )
            return

        # --- Paper-mode per-market cooldown ---
        # The idempotency key in execution_service_v2 includes price, so a
        # 1-tick price change generates a new key and bypasses dedup, causing
        # the same market to receive 2-3 orders in one 2-second scan window.
        # Guard against this with a 60-second cooldown per market+token.
        is_paper = self.config.get("trading", {}).get("paper_trading", True)
        if is_paper:
            _cooldown_key = f"{market_id}:{token_id}"
            _cooldown_secs = 60.0
            _now_mono = asyncio.get_running_loop().time()
            _last_submitted = self._paper_order_cooldowns.get(_cooldown_key, 0.0)
            if _now_mono - _last_submitted < _cooldown_secs:
                logger.debug(
                    "opportunity_skipped",
                    reason="paper_order_cooldown",
                    market_id=market_id,
                    token_id=token_id,
                    cooldown_remaining_seconds=round(_cooldown_secs - (_now_mono - _last_submitted), 1),
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

        min_price = from_config(trading_cfg.get("min_price", "0.01"))
        max_price = from_config(trading_cfg.get("max_price", "0.99"))
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

        max_position_pct = from_config(
            strategy_cfg.get(
                "max_position_size_pct",
                trading_cfg.get("max_position_size_pct", "5.0"),
            )
        )
        min_position_size = from_config(trading_cfg.get("min_position_size", "1.00"))
        max_order_size = from_config(trading_cfg.get("max_order_size", "1000.00"))

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
        # Allow up to 1 cent below min_position_size: quantize_quantity rounds
        # down so order_value can be $0.01 less than position_value even though
        # position_value already passed the >= min_position_size guard above.
        if order_value < min_position_size - Decimal("0.01"):
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
            logger.debug(
                "circuit_breaker_check",
                equity=str(equity),
                position_size_pct=round(position_size_pct, 2),
                cb_state=self.circuit_breaker.state.value,
                half_open_max_pct=self.circuit_breaker.half_open_max_position_pct,
                peak_equity=float(self.circuit_breaker.peak_equity),
                consecutive_losses=self.circuit_breaker.consecutive_losses,
            )
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
                    position_size_pct=round(position_size_pct, 2),
                    cb_state=self.circuit_breaker.state.value,
                    half_open_max_pct=self.circuit_breaker.half_open_max_position_pct,
                    peak_equity=float(self.circuit_breaker.peak_equity),
                    trigger=trigger,
                )
                return

        confidence = self._resolve_opportunity_confidence(opportunity.get("confidence"))

        # --- Charlie gate: mandatory signal check before any order -----------
        charlie_rec: Optional[TradeRecommendation] = None
        charlie_p_win: Optional[Decimal] = None
        charlie_conf_dec: Optional[Decimal] = None
        charlie_regime: Optional[str] = None
        observation_id: Optional[str] = None
        # Initialized here so the OFI block below is safe even when charlie_gate
        # is None (degraded startup / unit-test path).
        _extra_features: Optional[dict] = None

        if self.charlie_gate is not None:
            # Map opportunity asset to Charlie vocab (default BTC).
            # The opportunity dict uses "asset" (e.g. "SOL", "BTC") not "symbol".
            # Falling back to "symbol" / "btc_symbol" for legacy compatibility.
            opp_symbol = str(
                opportunity.get("asset")
                or opportunity.get("symbol")
                or opportunity.get("btc_symbol")
                or "BTC"
            )

            # Fetch real Binance technical features (cached 60s; returns None on failure)
            # This is what actually gives Charlie's ML models live market context.
            # Without it every model returns HOLD → p_win=0.5 → coin-flip trades.
            _extra_features = await asyncio.get_running_loop().run_in_executor(
                None, _get_binance_features, opp_symbol
            )
            if _extra_features is not None:
                logger.debug(
                    "binance_features_ready",
                    symbol=opp_symbol,
                    rsi_14=round(_extra_features.get("rsi_14", 0), 2),
                    macd=round(_extra_features.get("macd", 0), 6),
                    book_imbalance=round(_extra_features.get("book_imbalance", 0), 4),
                )
            else:
                logger.warning("binance_features_unavailable",
                               symbol=opp_symbol,
                               msg="Charlie will run in degraded mode — coin-flip rejection will block trade")

            # Charlie's edge model always treats market_price as the YES token
            # price.  When the strategy surfaces a NO opportunity it sets
            # market_price = no_token_price (the complement).  Passing that
            # directly inverts Charlie's edge calculation — it thinks YES is
            # cheap when it is actually expensive, and vice-versa.  Normalise
            # here so Charlie always receives the canonical YES price and can
            # independently choose the better side from the correct baseline.
            _opp_original_side = str(opportunity.get("side") or "YES").upper()
            _charlie_market_price = (
                price if _opp_original_side == "YES"
                else (Decimal("1") - price)
            )
            charlie_rec = await self._safe_await(
                "charlie_gate.evaluate_market",
                self.charlie_gate.evaluate_market(
                    market_id=market_id,
                    market_price=_charlie_market_price,
                    symbol=opp_symbol,
                    timeframe="15m",
                    bankroll=equity,
                    extra_features=_extra_features,
                    market_question=str(opportunity.get("question") or "")[:80],
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
                self._session_stats["blocked_charlie_rejected"] += 1
                return

            logger.info(
                "charlie_normalization_trace",
                market_id=market_id,
                opportunity_side=_opp_original_side,
                token_price=str(price),
                normalized_yes_price=str(_charlie_market_price),
                p_win_raw=round(float(charlie_rec.p_win_raw), 6),
                p_win_calibrated=round(float(charlie_rec.p_win_calibrated), 6),
                charlie_selected_side=charlie_rec.side,
                charlie_selected_side_p_win=round(float(charlie_rec.p_win), 6),
                trigger=trigger,
            )

            observation_id = await self._record_calibration_observation(
                market_id=market_id,
                token_id=token_id,
                opportunity=opportunity,
                charlie_rec=charlie_rec,
                token_price=price,
                normalized_yes_price=_charlie_market_price,
                trigger=trigger,
                observation_mode=(
                    "observe_only_bad_calibration" if observe_only_bad_calibration else "trade_enabled"
                ),
            )

            if charlie_rec.side != _opp_original_side:
                remapped_token_id = str(
                    opportunity.get("yes_token_id") if charlie_rec.side == "YES" else opportunity.get("no_token_id")
                ).strip()
                remapped_price_raw = (
                    opportunity.get("yes_price") if charlie_rec.side == "YES" else opportunity.get("no_price")
                )
                if not remapped_token_id or remapped_price_raw is None:
                    await self._update_calibration_observation(
                        observation_id,
                        guard_block_reason="charlie_side_mismatch",
                        training_eligibility="blocked_pre_execution",
                    )
                    logger.warning(
                        "blocked_by_charlie_side_mismatch",
                        market_id=market_id,
                        token_id=token_id,
                        opportunity_side=_opp_original_side,
                        charlie_side=charlie_rec.side,
                        trigger=trigger,
                    )
                    self._session_stats["blocked_charlie_rejected"] += 1
                    return

                token_id = remapped_token_id
                price = to_decimal(remapped_price_raw)
                logger.info(
                    "charlie_execution_target_remapped",
                    market_id=market_id,
                    opportunity_side=_opp_original_side,
                    charlie_side=charlie_rec.side,
                    token_id=token_id,
                    price=str(price),
                    trigger=trigger,
                )

            quarantine_entry = self._get_active_quarantine_entry(market_id)
            if quarantine_entry is not None:
                await self._update_calibration_observation(
                    observation_id,
                    guard_block_reason="quarantine",
                    training_eligibility="blocked_pre_execution",
                )
                logger.info(
                    "blocked_by_quarantine",
                    market_id=market_id,
                    trigger=trigger,
                    observation_id=observation_id,
                    **self.quarantine_repository.to_block_log_context(quarantine_entry),
                )
                self._session_stats["blocked_quarantine"] += 1
                self._session_stats["blocked_static_list"] += 1
                return

            if calibration_blocked:
                await self._update_calibration_observation(
                    observation_id,
                    guard_block_reason="bad_calibration",
                    training_eligibility="blocked_pre_execution",
                )
                logger.warning(
                    "blocked_by_bad_calibration",
                    market_id=market_id,
                    trigger=trigger,
                    observation_id=observation_id,
                    **self.guard_evaluator.calibration_block_log_context(
                        self._calibration_guard_status,
                        observe_only=observe_only_bad_calibration,
                    ),
                )
                self._session_stats["blocked_bad_calibration"] += 1
                if observe_only_bad_calibration:
                    self._session_stats["observe_only_bad_calibration"] += 1
                    logger.info(
                        "observe_only_bad_calibration",
                        market_id=market_id,
                        observation_id=observation_id,
                        trigger=trigger,
                    )
                    return
                return

            # Stamp the cooldown as soon as Charlie approves.  Without this,
            # budget-blocked markets re-trigger Charlie on every price tick since
            # the cooldown was previously only stamped after order_submitted.
            # Stamping here ensures the 60 s guard fires regardless of whether
            # the order ultimately clears the portfolio budget checks.
            if is_paper:
                self._paper_order_cooldowns[f"{market_id}:{token_id}"] = asyncio.get_running_loop().time()

            # Adopt Charlie's recommended side and Kelly size
            side = charlie_rec.side
            quantity = quantize_quantity(charlie_rec.size / price) if price > Decimal("0") else quantity
            order_value = quantize_quantity(quantity * price)
            charlie_p_win  = Decimal(str(charlie_rec.p_win))
            charlie_conf_dec = Decimal(str(charlie_rec.confidence))
            charlie_regime = charlie_rec.regime
            _charlie_p_win_raw = Decimal(str(charlie_rec.p_win_raw))
            _charlie_p_win_calibrated = Decimal(str(charlie_rec.p_win_calibrated))

            # --- Regime-based position-size multiplier (fallback only) -----
            # The new dynamic regime classifier (utils.regime_classifier) runs
            # every ~60 s and produces its own Kelly multiplier below.
            # This old REGIME_RISK_OVERRIDES path is kept ONLY as a fallback for
            # the window before the dynamic classifier has produced its first
            # result (self._dynamic_regime is None).
            # When the dynamic classifier is active, do NOT apply BOTH multipliers
            # — they would compound and could breach the portfolio risk cap.
            if self._dynamic_regime is None:
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
                        source="REGIME_RISK_OVERRIDES_fallback",
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
                    await self._update_calibration_observation(
                        observation_id,
                        guard_block_reason="risk_budget",
                        training_eligibility="blocked_pre_execution",
                    )
                    self._session_stats["blocked_risk_budget"] += 1
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
                    await self._update_calibration_observation(
                        observation_id,
                        guard_block_reason="risk_budget",
                        training_eligibility="blocked_pre_execution",
                    )
                    self._session_stats["blocked_risk_budget"] += 1
                    return

            # --- Per-market lifecycle guard ----------------------------------
            _blocked, _lifecycle_reason, _lifecycle_context = self._check_lifecycle_guard(
                market_id=market_id,
                side=side,
                token_price=price,
            )
            if _blocked:
                await self._update_calibration_observation(
                    observation_id,
                    guard_block_reason=_lifecycle_reason,
                    training_eligibility="blocked_pre_execution",
                )
                logger.warning(
                    self.guard_evaluator.lifecycle_block_event_name(_lifecycle_reason),
                    market_id=market_id,
                    trigger=trigger,
                    **_lifecycle_context,
                )
                if _lifecycle_reason == "side_flip_rule":
                    self._session_stats["blocked_side_flip_rule"] += 1
                else:
                    self._session_stats["blocked_lifecycle_guard"] += 1
                return

            # --- Institutional portfolio risk check (category + correlation) -
            if self.portfolio_risk_engine is not None:
                _open_pos = await self._safe_await(
                    "ledger.get_open_orders.portfolio_risk",
                    self.ledger.get_open_orders(),
                    default=[],
                )
                # Build position list with cost = size * price and question from notes
                _pos_list = []
                for _p in (_open_pos or []):
                    try:
                        _cost = Decimal(str(_p.get("size", 0))) * Decimal(str(_p.get("price", 0) or 1))
                    except Exception:
                        _cost = Decimal("0")
                    _notes = _p.get("notes", "") or ""
                    _question = ""
                    if "question=" in _notes:
                        try:
                            _question = _notes.split("question=")[1].split(" ")[0]
                        except Exception:
                            pass
                    _pos_list.append({
                        "market_id": _p.get("market_id", ""),
                        "cost": _cost,
                        "question": _question,
                    })
                _mkt_question = opportunity.get("question", "") or opportunity.get("market_question", "")
                _approved_size, _reject_reason = self.portfolio_risk_engine.check_and_size(
                    market_id=market_id,
                    market_question=str(_mkt_question),
                    kelly_size=order_value,
                    equity=equity,
                    open_positions=_pos_list,
                )
                _category = self.portfolio_risk_engine.categorize(str(_mkt_question))
                _cat_exposure = sum(_p["cost"] for _p in _pos_list
                                    if self.portfolio_risk_engine.categorize(_p["question"]) == _category)
                _total_exposure = sum(_p["cost"] for _p in _pos_list)
                logger.info(
                    "portfolio_risk_check",
                    market_id=market_id,
                    category=_category,
                    cat_exposure_pct=round(float(_cat_exposure / equity), 4) if equity else 0,
                    total_exposure_pct=round(float(_total_exposure / equity), 4) if equity else 0,
                    kelly_original=str(order_value),
                    kelly_approved=str(_approved_size),
                )
                if _reject_reason:
                    logger.warning(
                        "order_blocked_portfolio_risk",
                        market_id=market_id,
                        reason=_reject_reason,
                        trigger=trigger,
                    )
                    return
                if _approved_size < order_value:
                    order_value = _approved_size
                    quantity = quantize_quantity(order_value / price) if price > Decimal("0") else quantity

            # --- Session 2: dynamic regime size multiplier -------------------------
            # Scales the Kelly bet by the per-regime fractional_kelly relative to
            # the baseline KELLY_CONFIG value so trade size shrinks in event/high-vol
            # regimes and expands modestly in confirmed trend regimes.
            #
            # This multiplier REPLACES the old REGIME_RISK_OVERRIDES multiplier once
            # the dynamic classifier has produced its first result (_dynamic_regime is
            # not None).  The old override acts as a pre-init fallback only (above).
            # The two multipliers therefore NEVER compound.
            _dyn_regime = self._dynamic_regime or "calm"
            _regime_cfg = REGIME_RISK_CONFIG.get(_dyn_regime, REGIME_RISK_CONFIG["calm"])
            _default_kelly_frac = Decimal(str(KELLY_CONFIG.get("fractional_kelly", "0.25")))
            _regime_kelly_frac = Decimal(str(_regime_cfg.get("fractional_kelly", "0.25")))
            if _default_kelly_frac > Decimal("0"):
                _regime_mult = _regime_kelly_frac / _default_kelly_frac
            else:
                _regime_mult = Decimal("1.0")
            _regime_mult = min(_regime_mult, Decimal("1.5"))   # cap upside at 1.5×
            _regime_mult = max(_regime_mult, Decimal("0.10"))  # floor downside at 0.1×
            if _regime_mult != Decimal("1.0"):
                order_value = quantize_quantity(order_value * _regime_mult)
                quantity = quantize_quantity(order_value / price) if price > Decimal("0") else quantity
                logger.info(
                    "regime_size_adjustment",
                    event="regime_size_adjustment",
                    market_id=market_id,
                    regime=_dyn_regime,
                    regime_kelly_frac=str(_regime_kelly_frac),
                    multiplier=str(round(float(_regime_mult), 4)),
                    adjusted_order_value=str(order_value),
                )

            if order_value < min_position_size - Decimal("0.01"):
                logger.info(
                    "order_blocked_kelly_size_too_small",
                    market_id=market_id,
                    order_value=str(order_value),
                    min_position_size=str(min_position_size),
                    trigger=trigger,
                )
                return
        else:
            logger.error(
                "opportunity_blocked_charlie_unavailable",
                market_id=market_id,
                trigger=trigger,
            )
            self._session_stats["blocked_charlie_rejected"] += 1
            return

        # --- Absolute max entry-price filter (side-symmetric) ---------------
        # Block any bet — YES or NO — when the token being purchased costs more
        # than max_entry_price_abs (default 0.65).  Catches two failure modes:
        #
        #   YES token ≥ 0.65: market consensus already heavily prices in YES;
        #     limited upside with high calibration-error sensitivity.
        #
        #   NO token ≥ 0.65 (= YES token ≤ 0.35): the historically-bad trades
        #     on 2026-03-06 (e.g. buying NO at 0.73 with p(NO)=0.437) had
        #     direct negative EV.  This guard would have blocked them:
        #       price = no_token_price = 0.73 > 0.65 → blocked.
        #
        # Note: `price` is opportunity.market_price = the token-being-bought
        # price (no_price for NO opps, yes_price for YES opps), so this guard
        # is correctly side-symmetric without needing to know the side.
        # Configurable via trading.max_entry_price_abs (default 0.65).
        _max_entry_price = Decimal(
            str(self.config.get("trading", {}).get("max_entry_price_abs", "0.65"))
        )
        if price > _max_entry_price:
            await self._update_calibration_observation(
                observation_id,
                guard_block_reason="max_entry_price",
                training_eligibility="blocked_pre_execution",
            )
            logger.warning(
                "blocked_by_max_entry_price",
                market_id=market_id,
                side=side,
                token_price=str(price),
                max_entry_price=str(_max_entry_price),
                trigger=trigger,
            )
            self._session_stats["blocked_max_entry_price"] += 1
            return

        metadata = {
            "trigger": trigger,
            "outcome": side,
            "direction": str(opportunity.get("direction") or ("UP" if side == "YES" else "DOWN")),
            "edge": str(edge),
            "confidence": str(confidence),
            "question": str(opportunity.get("question") or ""),
            "btc_price": str(opportunity.get("btc_price")) if opportunity.get("btc_price") is not None else None,
            "charlie_p_win": str(charlie_p_win) if charlie_p_win is not None else None,
            "charlie_p_win_raw": str(_charlie_p_win_raw) if self.charlie_gate is not None else None,
            "charlie_p_win_calibrated_yes": str(_charlie_p_win_calibrated) if self.charlie_gate is not None else None,
            "charlie_yes_price_equiv": str(_charlie_market_price) if self.charlie_gate is not None else None,
            "opportunity_side": _opp_original_side if self.charlie_gate is not None else side,
            "charlie_confidence": str(charlie_conf_dec) if charlie_conf_dec is not None else None,
            "charlie_regime": charlie_regime,
            "charlie_edge": str(charlie_rec.edge) if charlie_rec is not None else None,
            "charlie_implied_prob": str(charlie_rec.implied_prob) if charlie_rec is not None else None,
        }

        # Idempotent dedup key: 1-minute window prevents duplicate submissions
        # when rapid price ticks fire multiple scans for the same opportunity.
        import hashlib as _hashlib
        from datetime import datetime as _dt, timezone as _tz
        _minute_str = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M")
        _dedup_input = f"{market_id}:{token_id}:{side}:{price}:{order_value}:{_minute_str}"
        _client_order_id = _hashlib.sha256(_dedup_input.encode()).hexdigest()[:16]
        metadata["client_order_id"] = _client_order_id
        metadata["expected_price"] = str(price)  # For slippage tracking on fill
        metadata["observation_id"] = observation_id
        _order_notes = None
        if charlie_rec is not None:
            _order_notes = (
                f"{charlie_rec.reason} "
                f"raw_yes_p={float(charlie_rec.p_win_raw):.6f} "
                f"cal_yes_p={float(charlie_rec.p_win_calibrated):.6f} "
                f"selected_side_p={float(charlie_rec.p_win):.6f} "
                f"normalized_yes_price={str(_charlie_market_price)} "
                f"token_price={str(price)} "
                f"opportunity_side={_opp_original_side} "
                f"observation_id={observation_id} "
                f"schema_version=2 feature_space=yes_side_raw_probability "
                f"label_space=yes_market_outcome"
            )

        # Write CREATED row to unified order ledger before sending to exchange.
        # Stores model_votes so per-model feedback works on settlement.
        pre_order_id = f"pre_{market_id}_{token_id}_{int(asyncio.get_running_loop().time()*1000)}"
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
                    notes=_order_notes,
                ),
            )

        # --- Session 4: OFI execution policy (log-only until offline-validated) ---
        # build_ofi_features + choose_execution_action are both fail-open.
        try:
            _ofi_exec_feats = _build_ofi_features(
                extra_features=_extra_features,
                volatility=float(charlie_rec.edge) if charlie_rec is not None else 0.0,
                time_to_expiry=float(opportunity.get("time_to_expiry") or 0),
            )
            _ofi_action, _ofi_exec_feats = _ofi_choose_action(_ofi_exec_feats)
            _log_ofi_action(logger, market_id, _ofi_action, _ofi_exec_feats)
            # OFI graduation: gated behind OFI_POLICY_ACTIVE so the path can be
            # enabled after offline Sharpe validation without another code change.
            if getattr(config_production, "OFI_POLICY_ACTIVE", False) and _ofi_action == "WAIT":
                logger.info(
                    "ofi_policy_deferred_order",
                    market_id=market_id,
                    action=_ofi_action,
                )
                return
        except Exception as _ofi_err:
            logger.warning("ofi_policy_log_failed", error=str(_ofi_err))

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
            charlie_p_win_raw=str(_charlie_p_win_raw) if self.charlie_gate is not None else None,
            charlie_p_win_calibrated_yes=str(_charlie_p_win_calibrated) if self.charlie_gate is not None else None,
            normalized_yes_price=str(_charlie_market_price) if self.charlie_gate is not None else None,
            opportunity_side=_opp_original_side if self.charlie_gate is not None else side,
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
                            notes=_order_notes,
                        ),
                    )
                    # pre_ row is superseded by the confirmed exchange order ID.
                    # Mark it terminal so get_open_orders() never returns it and
                    # the settlement loop cannot double-count PnL.
                    if pre_order_id:
                        await self._safe_await(
                            "ledger.transition_order_state_superseded",
                            self.ledger.transition_order_state(
                                pre_order_id, "SUPERSEDED",
                                notes=f"superseded_by={exchange_order_id}",
                            ),
                        )
                if self.ledger is not None:
                    await self._safe_await(
                        "ledger.transition_order_state_submitted",
                        self.ledger.transition_order_state(
                            exchange_order_id, new_state_str
                        ),
                    )
                await self._update_calibration_observation(
                    observation_id,
                    order_id=exchange_order_id,
                    training_eligibility="pending_resolution",
                )
                # --- Slippage tracking: record filled_price vs expected_price ---
                self._record_lifecycle_entry(
                    market_id=market_id,
                    side=side,
                    token_id=token_id,
                    token_price=price,
                    order_id=exchange_order_id,
                )
                _fill_price = (
                    getattr(result, "avg_price", None)
                    or getattr(result, "average_price", None)
                    or getattr(result, "fill_price", None)
                )
                if _fill_price is not None and self.ledger is not None:
                    try:
                        _expected = float(price)
                        _filled   = float(_fill_price)
                        _slip_bps = (_filled - _expected) / _expected * 10_000 if _expected else 0.0
                        await self._safe_await(
                            "ledger.update_slippage",
                            self.ledger.execute(
                                "UPDATE order_tracking SET filled_price=?, slippage_bps=?"
                                " WHERE order_id=?",
                                (_filled, round(_slip_bps, 2), exchange_order_id),
                            ),
                            timeout_seconds=3.0,
                        )
                        logger.info(
                            "slippage_recorded",
                            order_id=exchange_order_id,
                            expected_price=_expected,
                            filled_price=_filled,
                            slippage_bps=round(_slip_bps, 2),
                        )
                    except Exception as _se:
                        logger.warning("slippage_record_failed", error=str(_se))
            elif not result.success and pre_order_id:
                _err_msg = str(getattr(result, "error", "unknown"))
                _err_code = str(getattr(result, "error_code", "unknown"))
                logger.error(
                    "order_state_set_to_error",
                    pre_order_id=pre_order_id,
                    market_id=market_id,
                    token_id=token_id,
                    error=_err_msg,
                    error_code=_err_code,
                    trigger=trigger,
                )
                if self.ledger is not None:
                    await self._safe_await(
                        "ledger.transition_order_state_error",
                        self.ledger.transition_order_state(
                            pre_order_id, "ERROR",
                            notes=_err_msg,
                        ),
                    )
                await self._update_calibration_observation(
                    observation_id,
                    training_eligibility="not_executed",
                )

        if result is None or not result.success:
            await self._update_calibration_observation(
                observation_id,
                training_eligibility="not_executed",
            )
            _exec_err = getattr(result, "error", "null_result")
            _exec_code = getattr(result, "error_code", "null_result")
            _exec_status = (
                result.status.value
                if (result is not None and hasattr(result.status, "value"))
                else (str(result.status) if result is not None else "null_result")
            )
            logger.error(
                "execution_failed",
                market_id=market_id,
                token_id=token_id,
                error=_exec_err,
                error_code=_exec_code,
                status=_exec_status,
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
        self._session_stats["orders_submitted"] += 1
        # Cooldown and lifecycle lock already stamped earlier (Charlie approval).
        # Stamping again here was a leftover from before that fix and would
        # overwrite the key with a later timestamp — no semantic harm, but
        # it obscures intent.  The earlier stamp (inside the charlie_gate block)
        # is the authoritative one and guards all downstream budget checks.

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
            self.quarantine_repository = QuarantineRepository(self.ledger)
            self.calibration_observation_service = CalibrationObservationService(
                ledger=self.ledger,
                observation_export_path=str(self.calibration_observations_path),
                dataset_export_path=str(self.calibration_dataset_path),
                feature_schema_version=str(self.calibration_guard_config.get("meta_candidate_feature_schema_version", "meta_candidate_v1")),
                cluster_policy_version=str(self.calibration_guard_config.get("meta_candidate_cluster_policy_version", "cluster_v1")),
                cluster_time_bucket_seconds=int(self.calibration_guard_config.get("meta_candidate_cluster_time_bucket_seconds", 10)),
                cluster_price_bucket_abs=str(self.calibration_guard_config.get("meta_candidate_cluster_price_bucket_abs", "0.01")),
            )
            logger.info("ledger_initialized", path=db_path)
            await self._safe_await(
                "initialize_quarantine_store",
                self._initialize_quarantine_store(),
                timeout_seconds=25.0,
                default=None,
            )
            
            # Initialize with capital if needed
            equity = await self._await_step("ledger.get_equity", self.ledger.get_equity())
            initial_capital = Decimal(str(self.config.get('trading', {}).get('initial_capital', 10000)))
            if equity == Decimal('0'):
                await self._await_step(
                    "ledger.record_deposit",
                    self.ledger.record_deposit(initial_capital, "Initial capital")
                )
                logger.info("initial_capital_deposited", amount=str(initial_capital))
            elif paper_trading and equity < initial_capital:
                # In paper mode the ledger balance is virtual — if it has drained
                # below the configured starting level (e.g. from prior test runs),
                # top it back up so Kelly sizing has a real bankroll to work with.
                # This never runs in live mode, preserving real PnL history.
                shortfall = initial_capital - equity
                await self._await_step(
                    "ledger.record_deposit.paper_topup",
                    self.ledger.record_deposit(shortfall, f"Paper trading top-up (equity {equity} < initial_capital {initial_capital})")
                )
                logger.info(
                    "paper_equity_restored",
                    previous_equity=str(equity),
                    deposited=str(shortfall),
                    new_target=str(initial_capital),
                )

            if paper_trading:
                # Paper mode: force-close stale OPEN positions from prior paper runs
                # so portfolio_state.refresh() doesn't count them against the budget.
                #
                # SAFETY: Only close rows where mode='paper' (or mode IS NULL for
                # legacy rows that pre-date the mode column).  Rows where mode='live'
                # represent real on-chain positions and must NEVER be auto-closed by
                # a paper-mode startup — their P&L is real money.
                await self._await_step(
                    "ledger.close_stale_paper_positions",
                    self.ledger.execute(
                        "UPDATE positions SET status='CLOSED',"
                        " exit_timestamp=CURRENT_TIMESTAMP"
                        " WHERE status='OPEN'"
                        "   AND (mode IS NULL OR mode='paper')"
                    ),
                )
                logger.info("paper_positions_reset",
                            msg="stale OPEN paper positions force-closed at paper startup (live positions preserved)")

                # Expire stale order_tracking rows from prior paper sessions.
                # CREATED/SUBMITTED rows left by crashes are counted as active
                # exposure by portfolio_risk_engine (via get_open_orders()), which
                # can silently reject every new trade as over-exposed.  This must
                # run BEFORE the CB is initialized so the equity view is clean.
                _expired = await self._await_step(
                    "ledger.cancel_stale_paper_orders",
                    self.ledger.cancel_stale_paper_orders(),
                )
                logger.info(
                    "stale_paper_orders_cleaned",
                    expired_count=_expired,
                    msg="stale paper CREATED/SUBMITTED rows expired before CB init",
                )

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
            # 8. Kelly Sizer (OrderStore removed — order tracking lives in AsyncLedger)
            logger.info("component_construct_begin", component="kelly_sizer")
            self.kelly_sizer = KellySizer(config=KELLY_CONFIG)
            logger.info("component_construct_success", component="kelly_sizer")

            # 10. Charlie Prediction Gate
            charlie_min_edge = Decimal(
                str(os.getenv("CHARLIE_MIN_EDGE", CHARLIE_CONFIG.get("min_edge", Decimal("0.05"))))
            )
            charlie_min_conf = Decimal(
                str(os.getenv("CHARLIE_MIN_CONFIDENCE", CHARLIE_CONFIG.get("min_confidence", Decimal("0.60"))))
            )
            charlie_regimes = CHARLIE_CONFIG.get("allowed_regimes", None)
            charlie_timeout = float(CHARLIE_CONFIG.get("signal_timeout_seconds", 8.0))
            logger.info("component_construct_begin", component="charlie_gate")
            self.charlie_gate = CharliePredictionGate(
                kelly_sizer=self.kelly_sizer,
                min_edge=charlie_min_edge,
                min_confidence=charlie_min_conf,
                allowed_regimes=charlie_regimes,
                signal_timeout=charlie_timeout,
            )
            await self._await_step(
                "charlie_gate.verify_contract_health",
                self.charlie_gate.verify_contract_health(),
            )
            logger.info(
                "component_construct_success",
                component="charlie_gate",
                min_edge=str(charlie_min_edge),
                min_confidence=str(charlie_min_conf),
                contract_version=getattr(self.charlie_gate, "contract_version_expected", None),
                coin_flip_reject_band_abs=str(
                    getattr(self.charlie_gate, "_coin_flip_reject_band_abs", Decimal("0.03"))
                ),
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

            btc_price_scanner_cfg = self.config.get('strategies', {}).get('btc_price_scanner', {})
            btc_price_scanner_enabled = bool(btc_price_scanner_cfg.get('enabled', False))
            if btc_price_scanner_enabled:
                logger.info("component_construct_begin", component="btc_price_scanner")
                self.btc_price_scanner = BTCPriceLevelScanner(config=btc_price_scanner_cfg)
                logger.info("component_construct_success", component="btc_price_scanner")
            else:
                logger.warning("strategy_disabled", strategy="btc_price_scanner")

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

            # 14. Portfolio risk engine (category-aware exposure + drawdown kill switch)
            risk_cfg = self.config.get("risk", {})
            logger.info("component_construct_begin", component="portfolio_risk_engine")
            self.portfolio_risk_engine = PortfolioRiskEngine(config={
                "max_total_exposure_pct":    risk_cfg.get("max_total_exposure_pct",    0.30),
                "max_category_exposure_pct": risk_cfg.get("max_category_exposure_pct", 0.10),
                "max_single_market_pct":     risk_cfg.get("max_single_market_pct",     0.05),
                "max_same_asset_positions":  risk_cfg.get("max_same_asset_positions",  2),
                # Source from GLOBAL_RISK_BUDGET so it is explicit and reviewable.
                # Default falls back to portfolio_risk.py's own default (0.25).
                "min_tradeable_usdc":        float(GLOBAL_RISK_BUDGET.get("min_tradeable_usdc", 0.25)),
            })
            self.drawdown_monitor = DrawdownMonitor(
                max_drawdown_pct=float(risk_cfg.get("max_drawdown_pct", 15.0)) / 100.0
            )
            logger.info("component_construct_success", component="portfolio_risk_engine")

            # 15. Health HTTP server — serves GET /health on port 8765
            logger.info("component_construct_begin", component="health_server")
            self.health_server = HealthServer(state_ref=self)
            logger.info("component_construct_success", component="health_server")

            # 16. Binance @trade feed (per-fill granularity for sniper)
            _trade_feed_symbols = self.config.get('markets', {}).get('crypto_symbols', ['BTC', 'ETH'])
            logger.info("component_construct_begin", component="binance_trade_feed")
            self.binance_trade_feed = BinanceTradeFeed(symbols=_trade_feed_symbols)
            asyncio.get_running_loop().create_task(self.binance_trade_feed.run())
            logger.info(
                "component_construct_success",
                component="binance_trade_feed",
                symbols=_trade_feed_symbols,
            )

            # --- Calibration smoke test / fail-closed gate -----------------------
            try:
                self._calibration_guard_status = self._evaluate_calibration_guard()
                for _result in self._calibration_guard_status.get("smoke_results", []):
                    logger.info(
                        "calibration_smoke_test",
                        p_win_raw=round(_result["raw"], 4),
                        p_win_calibrated=round(_result["calibrated"], 4),
                        delta=round(_result["delta"], 4),
                    )

                if self._calibration_guard_status.get("coef") is not None:
                    logger.info(
                        "calibration_scaler_loaded",
                        coef=round(float(self._calibration_guard_status["coef"]), 6),
                        monotonic=bool(self._calibration_guard_status.get("monotonic")),
                    )

                if self._calibration_guard_status.get("blocked"):
                    logger.error(
                        "calibration_guard_blocking_trading",
                        reason=self._calibration_guard_status.get("reason"),
                        coef=self._calibration_guard_status.get("coef"),
                        monotonic=self._calibration_guard_status.get("monotonic"),
                    )
                elif not self._calibration_guard_status.get("scaler_exists"):
                    logger.warning(
                        "calibration_smoke_passthrough_warning",
                        msg="Scaler absent — calibration is passthrough until a valid scaler is fitted.",
                    )
            except Exception as _smoke_exc:
                logger.warning("calibration_smoke_test_failed", error=str(_smoke_exc))

            # --- Background LLM worker (fail-open) ----------------------------
            # Spawned last so all other components are live before the worker
            # starts consuming the event loop.  If LLM startup fails, trading
            # continues normally — the scanner will just get cache misses.
            try:
                import ai.llm_worker as _llm_worker_mod
                from ai.llm_worker import LLMWorker
                _llm_worker_mod._singleton_worker = LLMWorker()
                asyncio.get_running_loop().create_task(
                    _llm_worker_mod._singleton_worker.run()
                )
                logger.info("llm_worker_started")
            except Exception as _llm_start_err:
                logger.warning(
                    "llm_worker_start_failed", error=str(_llm_start_err)
                )

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
            # --- Load nightly Kelly optimizer config if present --------------
            import json as _json
            _kelly_live_path = Path("config/kelly_live.json")
            if _kelly_live_path.exists():
                try:
                    _live_cfg = _json.loads(_kelly_live_path.read_text())
                    if "min_edge_required" in _live_cfg:
                        KELLY_CONFIG["min_edge_required"] = Decimal(str(_live_cfg["min_edge_required"]))
                    if "fractional_kelly" in _live_cfg:
                        KELLY_CONFIG["fractional_kelly"] = Decimal(str(_live_cfg["fractional_kelly"]))
                    if "max_bet_pct" in _live_cfg:
                        KELLY_CONFIG["max_bet_pct"] = Decimal(str(_live_cfg["max_bet_pct"]))
                    logger.info(
                        "kelly_config_loaded_from_optimizer",
                        sharpe=_live_cfg.get("sharpe"),
                        trade_count=_live_cfg.get("trade_count"),
                        optimized_at=_live_cfg.get("optimized_at"),
                        min_edge=str(KELLY_CONFIG.get("min_edge_required")),
                        fractional_kelly=str(KELLY_CONFIG.get("fractional_kelly")),
                    )
                except Exception as _e:
                    logger.warning("kelly_live_config_load_failed", error=str(_e))

            # --- Load rolling Kelly snapshot (YAML) if present --------------
            # rolling_kelly_optimizer.py writes config/kelly_config_snapshot_{date}.yaml
            # and correctly uses KELLY_CONFIG's canonical keys (min_edge_required,
            # fractional_kelly, max_bet_pct).  The JSON loader above also now uses
            # the same canonical keys after the fix in commit 69f463a.
            import glob as _glob
            import yaml as _yaml
            _snap_files = sorted(
                _glob.glob(str(Path("config") / "kelly_config_snapshot_*.yaml"))
            )
            if _snap_files:
                _snap_path = _snap_files[-1]  # lexicographic = latest date
                try:
                    with open(_snap_path) as _f:
                        _snap = _yaml.safe_load(_f)
                    _best = _snap.get("best_combo", {})
                    if "min_edge_required" in _best:
                        KELLY_CONFIG["min_edge_required"] = Decimal(str(_best["min_edge_required"]))
                    if "fractional_kelly" in _best:
                        KELLY_CONFIG["fractional_kelly"] = Decimal(str(_best["fractional_kelly"]))
                    if "max_bet_pct" in _best:
                        KELLY_CONFIG["max_bet_pct"] = Decimal(str(_best["max_bet_pct"]))
                    _snap_meta = _snap.get("metrics", {})
                    logger.info(
                        "kelly_config_loaded_from_snapshot",
                        snapshot=str(_snap_path),
                        sharpe=_snap_meta.get("sharpe"),
                        trade_count=_snap_meta.get("trade_count"),
                        generated_at=_snap.get("generated_at"),
                        min_edge_required=str(KELLY_CONFIG.get("min_edge_required")),
                        fractional_kelly=str(KELLY_CONFIG.get("fractional_kelly")),
                        max_bet_pct=str(KELLY_CONFIG.get("max_bet_pct")),
                    )
                except Exception as _e:
                    logger.warning("kelly_snapshot_load_failed", path=_snap_path, error=str(_e))

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

            await self._reconcile_missing_open_orders_on_startup()
            await self._reconcile_positions_on_startup()

            # Refresh performance tracker after reconcile
            if self.performance_tracker is not None:
                await self._safe_await(
                    "performance_tracker.refresh_post_reconcile",
                    self.performance_tracker.refresh(),
                )

            await self._sync_lifecycle_state_from_open_orders()

            # --- Paper-mode peak_equity guard -----------------------------------
            # Each paper session starts fresh equity at STARTING_CAPITAL.  If the
            # performance tracker's equity-curve peak is from a prior session (and
            # is therefore higher than today's starting equity), the drawdown
            # calculation would immediately fire a halt.  In paper mode only we
            # reset peak_equity to the current equity so the session starts with a
            # clean baseline.  In live mode the peak is NEVER auto-reset.
            _is_paper = self.config.get("trading", {}).get("paper_trading", True)
            if _is_paper and self.performance_tracker is not None:
                _current_eq = self.performance_tracker._current_equity
                _peak_eq = self.performance_tracker._peak_equity
                if _peak_eq > _current_eq and _current_eq > Decimal("0"):
                    self.performance_tracker._peak_equity = _current_eq
                    logger.info(
                        "paper_peak_equity_reset",
                        previous_peak=str(_peak_eq),
                        reset_to=str(_current_eq),
                        reason="paper_session_fresh_start",
                    )
            if _is_paper and self.drawdown_monitor is not None:
                # DrawdownMonitor is constructed fresh each session with peak=0;
                # seed it with the real current equity so drawdown math is valid.
                if hasattr(self.drawdown_monitor, "peak_equity"):
                    _current_eq = (
                        self.performance_tracker._current_equity
                        if self.performance_tracker is not None
                        else Decimal("0")
                    )
                    if _current_eq > Decimal("0"):
                        self.drawdown_monitor.peak_equity = _current_eq
                        logger.info(
                            "paper_drawdown_monitor_seeded",
                            peak_equity=str(_current_eq),
                        )
            # -------------------------------------------------------------------

            # Paper mode: force circuit breaker to CLOSED so a stale OPEN state
            # from a prior session (e.g. old drawdown or win-rate trip) never
            # silently blocks a fresh paper session.  In live mode the circuit
            # breaker state is NEVER auto-reset — the operator must reset it.
            if _is_paper and self.circuit_breaker is not None:
                from risk.circuit_breaker_v2 import CircuitState as _CS
                if self.circuit_breaker.state != _CS.CLOSED:
                    await self._safe_await(
                        "circuit_breaker.paper_startup_reset",
                        self.circuit_breaker.manual_reset(),
                        default=None,
                    )
                    logger.info(
                        "paper_circuit_breaker_reset",
                        reason="fresh_paper_session_startup",
                    )
            # Mark paper session start so performance checks are session-scoped
            if _is_paper:
                from datetime import datetime as _dt, timezone as _tz
                self._paper_session_start = _dt.now(_tz.utc).isoformat()
                logger.info("paper_session_start_marked",
                            session_start=self._paper_session_start)
            # -------------------------------------------------------------------

            self.running = True

            logger.info(
                "trading_system_started",
                status="operational"
            )

            # Main loop — start health server as background task first
            if self.health_server is not None:
                asyncio.create_task(self.health_server.serve())
                logger.info("health_server_task_started", port=int(os.environ.get("HEALTH_PORT", "8765")))

            self._log_session_snapshot(force=True)

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
        loop = asyncio.get_running_loop()
        next_iteration_at = loop.time() + self.loop_tick_seconds
        snapshot_interval_seconds = float(self.runtime_controls.get("session_snapshot_interval_seconds", 60))
        next_session_snapshot_at = loop.time() + snapshot_interval_seconds
        
        while self.running:
            try:
                iteration += 1
                
                # Wait for shutdown or next iteration
                try:
                    wait_seconds = max(0.0, next_iteration_at - loop.time())
                    await asyncio.wait_for(
                        self.shutdown_event.wait(),
                        timeout=wait_seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    pass  # Continue normal operation

                now = loop.time()
                next_iteration_at += self.loop_tick_seconds
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

                if now >= next_session_snapshot_at:
                    self._log_session_snapshot()
                    while next_session_snapshot_at <= now:
                        next_session_snapshot_at += snapshot_interval_seconds

                if (now - self.last_market_probe_at) >= self.market_probe_interval_seconds:
                    self.last_market_probe_at = now
                    await self._market_discovery_probe()

                # Drawdown kill switch: check before every scan
                if self.drawdown_monitor is not None and self.ledger is not None:
                    _dd_equity = await self._safe_await(
                        "ledger.get_equity.drawdown_check",
                        self.ledger.get_equity(),
                        default=Decimal("0"),
                    )
                    _dd_equity = _dd_equity if isinstance(_dd_equity, Decimal) else Decimal(str(_dd_equity))
                    if not self.drawdown_monitor.update(_dd_equity, logger):
                        logger.warning("strategy_scan_skipped_drawdown_halt",
                                       equity=str(_dd_equity))
                        continue

                await self._run_strategy_scan(trigger="main_loop")

                # --- Phase 3-6: arb scanner + oracle monitor on discovered markets ---
                if self.last_discovered_markets:
                    # Yes/No sum arb (risk-free) — log only
                    try:
                        _arb_opps = scan_yes_no_arb(
                            self.last_discovered_markets, self.api_client
                        )
                        if _arb_opps:
                            logger.info(
                                "arb_scan_complete",
                                opportunities_found=len(_arb_opps),
                                best_net_arb_pct=_arb_opps[0]["net_arb_pct"],
                            )
                    except Exception as _arb_exc:
                        logger.warning("arb_scan_failed", error=str(_arb_exc))

                    # Oracle window monitor — log only
                    try:
                        for _mkt in self.last_discovered_markets:
                            check_oracle_window(_mkt)
                    except Exception as _oracle_exc:
                        logger.warning("oracle_scan_failed", error=str(_oracle_exc))

                    # Last-second sniper check — log only
                    if self.market_sniper:
                        for _mkt in self.last_discovered_markets:
                            try:
                                _secs = self.market_sniper.seconds_to_close(_mkt)
                                if 0 < _secs <= 30:  # within 30s of close
                                    _btc_price = (
                                        self.binance_trade_feed.get_price("BTC")
                                        if self.binance_trade_feed
                                        else None
                                    )
                                    if _btc_price is not None:
                                        _mkt_price = Decimal(
                                            str(_mkt.get("market_price") or "0.50")
                                        )
                                        # TODO(sniper): wire real Charlie p_win here once the
                                        # signal is available on the sniper path.  Using 0.5
                                        # means should_snipe() operates on a coin-flip prior,
                                        # silently affecting size decisions.  Either inject a
                                        # real signal or gate behind a feature flag.
                                        _p_win = Decimal("0.5")  # placeholder — see TODO above
                                        if self.market_sniper.should_snipe(
                                            _mkt, _p_win, _mkt_price
                                        ):
                                            self.market_sniper.evaluate_snipe(
                                                _mkt, "YES", _p_win, _mkt_price
                                            )
                            except Exception as _snipe_exc:
                                logger.debug(
                                    "snipe_check_error", error=str(_snipe_exc)
                                )
                
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
            equity = Decimal("0")   # Safe default — prevents UnboundLocalError if circuit_breaker is None
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

            # --- Operator reset flag (live-mode CB reset without restart) ----
            # scripts/reset_circuit_breaker.py writes runtime/cb_reset.flag.
            # We consume it here, call manual_reset(), and delete the file so
            # it fires exactly once.  Paper mode uses the startup auto-reset;
            # this path exists for live-mode operator intervention.
            _cb_flag = Path("runtime/cb_reset.flag")
            if _cb_flag.exists() and self.circuit_breaker is not None:
                try:
                    await self._safe_await(
                        "circuit_breaker.operator_manual_reset",
                        self.circuit_breaker.manual_reset(),
                        default=None,
                    )
                    _cb_flag.unlink(missing_ok=True)
                    logger.warning(
                        "circuit_breaker_manual_reset",
                        source="operator_flag_file",
                        flag_path=str(_cb_flag),
                    )
                except Exception as _cb_flag_exc:
                    logger.error(
                        "circuit_breaker_flag_reset_failed",
                        error=str(_cb_flag_exc),
                    )
            # ------------------------------------------------------------------

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
                    _is_paper_mode = self.config.get("trading", {}).get("paper_trading", True)
                    if _is_paper_mode:
                        # Paper mode: warn only — the performance tracker's peak_equity
                        # is built from the cumulative all-session equity curve, so a prior
                        # session peak of e.g. $36k against today's $10k starting capital
                        # produces a 73% "drawdown" that is purely historical artefact.
                        # The CB already enforces session-scoped drawdown from initial_equity;
                        # firing a cross-session halt here would trip it on every paper start.
                        logger.warning(
                            "performance_halt_drawdown",
                            current_drawdown_pct=str(current_dd * 100),
                            threshold_pct=str(Decimal(str(max_dd_halt)) * 100),
                            paper_mode=True,
                            action="log_only",
                        )
                    else:
                        logger.critical(
                            "performance_halt_drawdown",
                            current_drawdown_pct=str(current_dd * 100),
                            threshold_pct=str(Decimal(str(max_dd_halt)) * 100),
                            paper_mode=False,
                            action="circuit_breaker_trip",
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
                    _is_paper_mode = self.config.get("trading", {}).get("paper_trading", True)
                    if _is_paper_mode:
                        # Paper mode: log only — the settled-trade history spans
                        # multiple sessions and predates config changes made
                        # between sessions.  Tripping here re-blocks a fresh
                        # paper session on every periodic_check cycle, immediately
                        # undoing the paper startup manual_reset().
                        # The CB already enforces session-scoped drawdown and
                        # loss-streak limits; cross-session win-rate is informational.
                        logger.critical(
                            "performance_halt_win_rate",
                            rolling_win_rate=f"{rolling_wr:.2%}",
                            threshold=str(min_wr),
                            sample_size=win_rate_sample,
                            paper_mode=True,
                            action="log_only",
                        )
                    else:
                        logger.critical(
                            "performance_halt_win_rate",
                            rolling_win_rate=f"{rolling_wr:.2%}",
                            threshold=str(min_wr),
                            sample_size=win_rate_sample,
                            paper_mode=False,
                            action="circuit_breaker_trip",
                        )
                        if self.circuit_breaker:
                            from risk.circuit_breaker_v2 import TripReason
                            await self._safe_await(
                                "circuit_breaker.trip_low_win_rate",
                                self.circuit_breaker.trip(TripReason.LOW_WIN_RATE),
                                default=None,
                            )

        except Exception as e:
            logger.error(
                "periodic_check_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
    
    async def _periodic_maintenance(self):
        """Periodic maintenance tasks."""
        try:
            # --- Session 2: volatility / regime update -------------------------
            # Runs off the hot path (~every 60 s).  All I/O (Binance REST 1-min
            # candles + SQLite PnL stats) is done in a thread pool so the event
            # loop is never blocked.  Regime transitions logged as regime_changed.
            try:
                _regime_feats = await asyncio.get_running_loop().run_in_executor(
                    None, _get_regime_features, None
                )
                _new_regime = _classify_regime(_regime_feats)
                if _new_regime != self._dynamic_regime:
                    logger.info(
                        "regime_changed",
                        event="regime_changed",
                        old_regime=self._dynamic_regime,
                        new_regime=_new_regime,
                        vol_5min=round(_regime_feats.get("vol_5min", 0) or 0, 5),
                        vol_60min=round(_regime_feats.get("vol_60min", 0) or 0, 5),
                        rsi_14=round(_regime_feats.get("rsi_14", 50) or 50, 1),
                    )
                self._dynamic_regime = _new_regime
            except Exception as _re:
                logger.warning("regime_update_failed", error=str(_re))

            # --- Meta-gate model hot-reload: check if model file was updated ----
            # If the model on disk is newer than the last time we loaded it, reset
            # the module-level cache so the next should_trade() call re-reads it.
            # Allows live retraining without a bot restart.
            try:
                from ml.meta_gate import _MODEL_PATH as _mgp, reload_model as _reload_meta
                if _mgp.exists():
                    _mgp_mtime = _mgp.stat().st_mtime
                    if getattr(self._periodic_maintenance, "_last_model_mtime", 0) < _mgp_mtime:
                        _reload_meta()
                        self._periodic_maintenance._last_model_mtime = _mgp_mtime  # type: ignore[attr-defined]
                        logger.info("meta_gate_model_hot_reloaded", mtime=_mgp_mtime)
            except Exception as _mr_exc:
                logger.warning("meta_gate_hot_reload_check_failed", error=str(_mr_exc))

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
                # Use a generous fixed timeout: with 190+ open orders the scan
                # makes one Gamma API call per unique market (throttled ~0.6 s
                # each).  20 s (network_timeout_seconds default) is far too short
                # — the scan gets killed every cycle and never completes.
                timeout_seconds=300.0,
                default=None,
            )
            await self._safe_await(
                "resolve_pending_calibration_observations",
                self._resolve_pending_calibration_observations(),
                timeout_seconds=120.0,
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

            # --- Cancel stale SUBMITTED orders (> 10 min unmatched) -------------
            await self._safe_await(
                "cancel_stale_orders",
                self._cancel_stale_orders(stale_after_seconds=600),
                timeout_seconds=60.0,
                default=None,
            )
            
            # Clear cache
            if self.secrets_manager:
                self.secrets_manager.clear_cache()

            # --- Schema integrity guard: warn if hot-path index is missing ----
            # Guards against ledger_async.py embedded schema drifting from
            # schema.sql.  A missing index silently degrades settlement query
            # performance without raising any error.
            if self.ledger:
                try:
                    _idx_rows = await self._safe_await(
                        "ledger.check_settled_closed_index",
                        self.ledger.execute(
                            "SELECT name FROM sqlite_master WHERE type='index'"
                            " AND name='idx_ot_settled_closed'",
                            fetch_all=True,
                            as_dict=True,
                        ),
                        timeout_seconds=5.0,
                        default=None,
                    )
                    if _idx_rows is not None and not _idx_rows:
                        logger.warning(
                            "missing_db_index",
                            index="idx_ot_settled_closed",
                            hint="Schema drift detected: apply index manually or"
                                 " restart to allow schema init to recreate it",
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.error(
                "periodic_maintenance_failed",
                error=str(e)
            )

    async def _cancel_stale_orders(self, stale_after_seconds: int = 600) -> None:
        """
        Cancel any order_tracking rows in SUBMITTED state older than
        ``stale_after_seconds`` seconds (default 10 min).

        Steps
        -----
        1. Query order_tracking for stale SUBMITTED rows.
        2. For each, send DELETE /order/{id} to the CLOB.
        3. Transition state → CANCELLED in the ledger.

        Rationale: unmatched limit orders sitting for > 10 min imply the
        market has moved away from our price.  Cancelling frees risk budget
        and prevents phantom exposure accumulating in the portfolio engine.
        """
        if self.ledger is None:
            return

        from datetime import datetime, timezone, timedelta
        cutoff_iso = (
            datetime.now(timezone.utc)
            - timedelta(seconds=stale_after_seconds)
        ).isoformat()

        stale_rows = await self._safe_await(
            "ledger.get_stale_submitted_orders",
            self.ledger.execute(
                "SELECT order_id, market_id FROM order_tracking"
                " WHERE order_state='SUBMITTED'"
                " AND opened_at < ?",
                (cutoff_iso,),
                fetch_all=True,
                as_dict=True,
            ),
            timeout_seconds=5.0,
            default=[],
        )
        if not stale_rows:
            return

        cancelled_ids: list = []
        for row in stale_rows:
            order_id = row.get("order_id", "") if isinstance(row, dict) else row[0]
            if not order_id or order_id.startswith("paper_"):
                # Skip paper orders — they have no real CLOB entry to cancel
                await self._safe_await(
                    f"ledger.cancel_paper_stale.{order_id}",
                    self.ledger.transition_order_state(order_id, "CANCELLED",
                                                       notes="stale_paper_order"),
                    timeout_seconds=3.0,
                )
                cancelled_ids.append(order_id)
                continue

            # Attempt CLOB cancellation for real orders
            cancel_ok = False
            if self.api_client and hasattr(self.api_client, "cancel_order"):
                try:
                    cancel_ok = await asyncio.wait_for(
                        self.api_client.cancel_order(order_id), timeout=8.0
                    )
                except Exception as exc:
                    logger.warning("cancel_order_api_error", order_id=order_id,
                                   error=str(exc))
            # Transition ledger regardless (even if CLOB call failed, the order
            # is stale and we no longer want to track it as open exposure)
            await self._safe_await(
                f"ledger.transition_cancelled.{order_id}",
                self.ledger.transition_order_state(
                    order_id, "CANCELLED",
                    notes=f"stale_order_cancelled clob_ok={cancel_ok}",
                ),
                timeout_seconds=3.0,
            )
            cancelled_ids.append(order_id)

        if cancelled_ids:
            logger.info(
                "stale_orders_cancelled",
                count=len(cancelled_ids),
                order_ids=cancelled_ids,
                stale_threshold_s=stale_after_seconds,
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

        logger.info("settlement_scan_begin")

        open_orders = await self._safe_await(
            "ledger.get_open_orders.settlement_poll",
            self.ledger.get_open_orders(),
            timeout_seconds=10.0,
            default=[],
        )
        if not open_orders:
            logger.info(
                "settlement_scan_complete",
                open_positions_checked=0,
                resolved_count=0,
                settled_order_ids=[],
            )
            return

        # De-duplicate market_id lookups so we don't hammer the API for every
        # order when multiple orders are open in the same market.
        market_cache: Dict[str, Any] = {}
        settled_count = 0
        settled_order_ids: list = []

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
            # Resolution check: handle both CLOB (closed/resolved bool) and Gamma
            # API (active=False) response formats.
            # GUARD: resolutionTime alone is not sufficient — markets carry a
            # scheduled timestamp before they actually resolve, and active=False
            # can mean "paused" rather than "resolved".  Require _outcome_posted
            # as a second condition to prevent premature settlement and wrong PnL.
            import time as _time
            _res_ts = market.get("resolutionTime")
            _resolved_in_past = (
                _res_ts is not None
                and isinstance(_res_ts, (int, float))
                and _res_ts <= _time.time()
            )
            _outcome_posted = bool(
                market.get("outcomePrices")
                or market.get("payout_numerator")
                or market.get("payout_per_share")
            )
            market_resolved = bool(
                market.get("closed")
                or market.get("resolved")
                or (market.get("active") is False and _outcome_posted)
                or (_resolved_in_past and _outcome_posted)
            )
            if not market_resolved:
                continue

            # Compute PnL using the same formula as reconcile_open_orders.
            # Check CLOB fields first, then Gamma API outcomePrices/outcomes.
            raw_payout = market.get("payout_numerator") or market.get("payout_per_share")
            if raw_payout is None:
                # Gamma API: outcomePrices=["0","1"] paired with outcomes=["Up","Down"].
                # Note: Gamma encodes outcomePrices as a JSON string, not an array —
                # must json.loads() it before indexing.
                # order_tracking stores outcome as "YES"/"NO" which always maps to the
                # FIRST/SECOND token in Polymarket binary markets (YES=Up=index 0,
                # NO=Down=index 1).  Use positional mapping, not string matching, to
                # avoid "YES" vs "Up" mismatch causing ValueError.
                import json as _json
                outcome_prices_raw = market.get("outcomePrices")
                order_outcome = (row.get("outcome") or "").strip().upper()  # YES or NO
                if outcome_prices_raw and order_outcome:
                    try:
                        outcome_prices = (
                            _json.loads(outcome_prices_raw)
                            if isinstance(outcome_prices_raw, str)
                            else outcome_prices_raw
                        )
                        if order_outcome in {"YES", "UP"}:
                            raw_payout = outcome_prices[0]
                        elif order_outcome in {"NO", "DOWN"}:
                            raw_payout = outcome_prices[1]
                    except (IndexError, ValueError, TypeError):
                        raw_payout = None
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
            # Release exposure: close any OPEN positions rows for this market so
            # portfolio_state.refresh() stops counting them against the risk budget.
            if market_id:
                await self._safe_await(
                    f"ledger.close_positions_for_market.{market_id}",
                    self.ledger.execute(
                        "UPDATE positions SET status='CLOSED',"
                        " exit_timestamp=CURRENT_TIMESTAMP"
                        " WHERE market_id=? AND status='OPEN'",
                        (market_id,),
                    ),
                    timeout_seconds=5.0,
                )
                self._clear_lifecycle_entry(market_id, reason="market_settled")
            settled_count += 1
            settled_order_ids.append(order_id)
            logger.info(
                "order_settled_live",
                order_id=order_id,
                market_id=market_id,
                pnl=str(pnl),
                winning_side=winning_side,
                payout_per_share=str(payout_per_share),
            )

            # --- Calibration data collection: schema-versioned YES-side dataset --
            _notes = str(row.get("notes") or "")
            _raw_yes_p = self._parse_note_token(_notes, "raw_yes_p")
            _cal_yes_p = self._parse_note_token(_notes, "cal_yes_p")
            _selected_side_p = self._parse_note_token(_notes, "selected_side_p")
            _normalized_yes_price = self._parse_note_token(_notes, "normalized_yes_price")
            _token_price = self._parse_note_token(_notes, "token_price")
            _observation_id = self._parse_note_token(_notes, "observation_id")
            _trade_side = str(row.get("outcome") or "")
            _actual_yes_outcome = 1 if str(winning_side).upper() == "YES" else 0
            _trade_outcome = 1 if pnl > Decimal("0") else 0
            if _observation_id:
                logger.debug(
                    "calibration_data_deferred_to_observation_resolver",
                    market_id=market_id,
                    order_id=order_id,
                    observation_id=_observation_id,
                )
            elif (
                self.calibration_observation_service is not None
                and _raw_yes_p is not None
                and _cal_yes_p is not None
            ):
                try:
                    await self.calibration_observation_service.record_settled_trade_fallback(
                        market_id=market_id,
                        order_id=order_id,
                        signal_side=_trade_side,
                        selected_side=_trade_side,
                        raw_yes_prob=str(round(float(_raw_yes_p), 6)),
                        calibrated_yes_prob=str(round(float(_cal_yes_p), 6)),
                        selected_side_prob=(
                            str(round(float(_selected_side_p), 6))
                            if _selected_side_p is not None
                            else ""
                        ),
                        token_price=_token_price or "",
                        normalized_yes_price=_normalized_yes_price or "",
                        timestamp=str(row.get("opened_at") or ""),
                        resolution_time=str(row.get("closed_at") or ""),
                        actual_yes_outcome=_actual_yes_outcome,
                        trade_outcome=_trade_outcome,
                    )
                    logger.debug(
                        "calibration_data_appended",
                        market_id=market_id,
                        raw_yes_prob=float(_raw_yes_p),
                        calibrated_yes_prob=float(_cal_yes_p),
                        actual_yes_outcome=_actual_yes_outcome,
                        trade_outcome=_trade_outcome,
                    )
                except Exception as _cal_exc:
                    logger.warning(
                        "calibration_data_append_failed",
                        error=str(_cal_exc),
                    )
            elif row.get("charlie_p_win") is not None:
                logger.warning(
                    "calibration_data_skipped_legacy_schema",
                    market_id=market_id,
                    order_id=order_id,
                    reason="missing_raw_yes_p_or_cal_yes_p_in_notes",
                )

        logger.info(
            "settlement_scan_complete",
            open_positions_checked=len(open_orders),
            resolved_count=settled_count,
            settled_order_ids=settled_order_ids,
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
                    timeout_seconds=45.0,
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
            
            # --- Session summary -------------------------------------------
            # Emit structured counts for the current session.  Separated from
            # the shutdown snapshot (which reads the DB) so it is always
            # logged even if the ledger is unavailable.
            logger.info(
                "session_summary",
                opportunities_evaluated=self._session_stats.get("opportunities_evaluated", 0),
                orders_submitted=self._session_stats.get("orders_submitted", 0),
                blocked_by_blocklist=self._session_stats.get("blocked_static_list", 0),
                blocked_by_quarantine=self._session_stats.get("blocked_quarantine", 0),
                blocked_by_lifecycle_guard=self._session_stats.get("blocked_lifecycle_guard", 0),
                blocked_by_side_flip_rule=self._session_stats.get("blocked_side_flip_rule", 0),
                blocked_charlie_rejected=self._session_stats.get("blocked_charlie_rejected", 0),
                blocked_meta_gate=self._session_stats.get("blocked_meta_gate", 0),
                blocked_risk_budget=self._session_stats.get("blocked_risk_budget", 0),
                blocked_by_max_entry_price=self._session_stats.get("blocked_max_entry_price", 0),
                blocked_by_bad_calibration=self._session_stats.get("blocked_bad_calibration", 0),
                observe_only_bad_calibration=self._session_stats.get("observe_only_bad_calibration", 0),
                markets_lifecycle_locked=len(self._market_lifecycle_state),
            )

            logger.info("trading_system_stopped")
            
        except Exception as e:
            logger.error(
                "shutdown_error",
                error=str(e),
                error_type=type(e).__name__
            )


async def main():
    """Main entry point."""
    # Prevent two bot instances from running simultaneously.
    # Two instances sharing separate in-memory cooldown dicts would double-fire
    # the same orders within the same second, bypassing per-market rate limits.
    acquire_instance_lock()

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
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable verbose debug logging; forces ConsoleRenderer even in live mode.'
    )
    args = parser.parse_args()

    # JSON logging is active when:
    #   a) --mode live and NOT --debug  (automatic in production), OR
    #   b) LOG_FORMAT=json env var is set (explicit override, e.g. paper-run CI checks)
    # All other cases (paper, replay, local dev) default to ConsoleRenderer.
    _use_json = (
        (args.mode == "live" and not args.debug)
        or os.environ.get("LOG_FORMAT", "").lower() == "json"
    )

    _log_handlers: list = [logging.StreamHandler(sys.stderr)]
    # Always write a log file regardless of mode:
    #   live mode  → logs/production.log  (JSON lines)
    #   paper mode → logs/paper.log       (ConsoleRenderer text)
    # Previously the file handler was gated on _use_json, so paper-mode output
    # went to stderr only — making the log file appear stale or empty.
    Path("logs").mkdir(parents=True, exist_ok=True)
    _log_file_name = "logs/production.log" if _use_json else "logs/paper.log"
    _log_handlers.append(
        logging.FileHandler(_log_file_name, encoding="utf-8")
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=_log_handlers,
        force=True,
    )

    # Configure structlog — JSONRenderer for live mode (auto) or LOG_FORMAT=json (explicit).
    # ConsoleRenderer is used for local dev, paper mode, and explicit --debug sessions.
    _final_renderer = (
        structlog.processors.JSONRenderer()
        if _use_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _final_renderer,
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
    config = _resolve_runtime_controls(config)
    
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
    logger.info(
        "runtime_controls_loaded",
        blocked_markets=config["runtime_controls"]["blocked_markets"],
        blocked_markets_count=len(config["runtime_controls"]["blocked_markets"]),
        lifecycle_guard=config["runtime_controls"]["lifecycle_guard"],
        max_entry_price_abs=config["runtime_controls"]["max_entry_price_abs"],
        calibration=config["runtime_controls"]["calibration"],
        quarantine=config["runtime_controls"]["quarantine"],
        session_snapshot_interval_seconds=config["runtime_controls"]["session_snapshot_interval_seconds"],
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


TradingBot = TradingSystem

import builtins as _builtins
if not hasattr(_builtins, "TradingBot"):
    _builtins.TradingBot = TradingSystem


if __name__ == '__main__':
    asyncio.run(main())
