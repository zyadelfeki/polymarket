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
        metadata = {
            "trigger": trigger,
            "outcome": side,
            "direction": str(opportunity.get("direction") or ("UP" if side == "YES" else "DOWN")),
            "edge": str(edge),
            "confidence": str(confidence),
            "question": str(opportunity.get("question") or ""),
            "btc_price": str(opportunity.get("btc_price")) if opportunity.get("btc_price") is not None else None,
        }

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
                }
            )
            logger.info("component_construct_success", component="execution_service")
            await self._await_step("execution_service.start", self.execution.start())

            # 6.5 Strategy Engine
            strategy_cfg = self.config.get('strategies', {}).get('latency_arb', {})
            strategy_enabled = bool(strategy_cfg.get('enabled', True))
            if strategy_enabled:
                logger.info("component_construct_begin", component="latency_arb_strategy")
                self.strategy_engine = MultiTimeframeLatencyArbitrageEngine(
                    binance_ws=self.websocket,
                    polymarket_client=self.api_client,
                    charlie_predictor=None,
                    config=strategy_cfg,
                    execution_service=None,
                    kelly_sizer=None,
                    redis_subscriber=None,
                )
                logger.info("component_construct_success", component="latency_arb_strategy")
            else:
                logger.warning("strategy_disabled", strategy="latency_arb")
            
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
    
    async def stop(self):
        """Stop the trading system."""
        logger.info("stopping_trading_system")
        
        self.running = False
        self.shutdown_event.set()
        
        try:
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
        choices=['paper', 'live'],
        default='paper',
        help='Trading mode'
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
