#!/usr/bin/env python3
"""
Latency Arbitrage Strategy

Exploits pricing delays between Binance spot prices and Polymarket prediction odds.

Strategy Logic:
1. Monitor BTC price on Binance (real-time WebSocket)
2. Poll Polymarket "BTC to 100K" market odds
3. Calculate implied probability from BTC price
4. When spread > threshold: Execute trade
5. Circuit breaker prevents runaway losses

Profit Source:
- Binance updates instantly (sub-second)
- Polymarket lags (human traders)
- Window: 2-10 seconds of exploitable mispricing
"""

import asyncio
from typing import Optional, Dict
from decimal import Decimal
from datetime import datetime, timedelta
try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

from data_feeds.binance_websocket_v2 import BinanceWebSocketV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from services.execution_service_v2 import ExecutionServiceV2
from services.correlation_context import CorrelationContext
from utils.correlation_id import generate_correlation_id
from risk.circuit_breaker_v2 import CircuitBreakerV2
from database.ledger_async import AsyncLedger
from services.strategy_health import StrategyHealthMonitor
from utils.decimal_helpers import to_decimal, quantize_price, quantize_quantity, to_timeout_float

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging
    from services.correlation_context import inject_correlation

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            kwargs = inject_correlation(kwargs)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

    logger = _FallbackLogger(__name__)


class LatencyArbitrageEngine:
    """
    Production latency arbitrage strategy.
    
    Monitors Binance BTC price and Polymarket odds for the
    "BTC to 100K by [date]" market, executing when mispricing detected.
    """
    
    def __init__(
        self,
        ledger: AsyncLedger,
        polymarket_client: PolymarketClientV2,
        execution_service: ExecutionServiceV2,
        circuit_breaker: CircuitBreakerV2,
        config: Optional[Dict] = None
    ):
        """
        Initialize strategy.
        
        Args:
            ledger: Database ledger
            polymarket_client: Polymarket API client
            execution_service: Order execution service
            circuit_breaker: Risk management
            config: Strategy configuration
        """
        self.ledger = ledger
        self.polymarket_client = polymarket_client
        self.execution = execution_service
        self.circuit_breaker = circuit_breaker
        
        # Configuration
        self.config = config or {}
        self.market_id = self.config.get('market_id', 'btc_to_100k')
        self.token_id = self.config.get('token_id')
        self.min_spread_bps = to_decimal(self.config.get('min_spread_bps', '50'))  # 0.5%
        self.max_spread_bps = to_decimal(self.config.get('max_spread_bps', '500'))  # 5% (sanity)
        self.max_position_pct = to_decimal(self.config.get('max_position_pct', '10.0'))  # 10% of equity
        self.poll_interval = to_decimal(self.config.get('poll_interval', '2.0'))  # Poll Polymarket every 2s
        self.btc_target = to_decimal(self.config.get('btc_target', '100000'))  # Target price
        self.fee_rate = to_decimal(self.config.get('fee_rate', '0.02'))
        self.min_profit_buffer_pct = to_decimal(self.config.get('min_profit_buffer_pct', '0.05'))
        self.health_pause_seconds = int(self.config.get('health_pause_seconds', 3600))
        
        # State
        self.running = False
        self.binance_ws: Optional[BinanceWebSocketV2] = None
        self.latest_btc_price: Optional[Decimal] = None
        self.latest_btc_timestamp: Optional[datetime] = None
        self.latest_polymarket_odds: Optional[Decimal] = None
        self.latest_polymarket_timestamp: Optional[datetime] = None
        self.yes_token_id: Optional[str] = None
        self.no_token_id: Optional[str] = None
        self.latest_spread: Optional[Decimal] = None
        self._paused_until: Optional[datetime] = None

        self.strategy_health = StrategyHealthMonitor("latency_arbitrage")
        
        # Metrics
        self.signals_generated = 0
        self.trades_executed = 0
        self.trades_blocked = 0
        
        logger.info(
            "latency_arbitrage_initialized",
            market_id=self.market_id,
            min_spread_bps=self.min_spread_bps,
            max_spread_bps=self.max_spread_bps,
            max_position_pct=self.max_position_pct
        )
    
    async def start(self):
        """Start strategy execution."""
        logger.info("starting_latency_arbitrage_strategy")
        
        self.running = True
        
        # Start Binance WebSocket
        self.binance_ws = BinanceWebSocketV2(
            symbols=['BTC'],
            on_price_update=self._on_binance_price
        )
        await self.binance_ws.start()
        
        logger.info(
            "strategy_started",
            market=self.market_id,
            binance_feed="active"
        )
        
        # Start main loop
        await self._strategy_loop()
    
    async def stop(self):
        """Stop strategy execution."""
        logger.info("stopping_latency_arbitrage_strategy")
        
        self.running = False
        
        if self.binance_ws:
            await self.binance_ws.stop()
        
        logger.info(
            "strategy_stopped",
            signals_generated=self.signals_generated,
            trades_executed=self.trades_executed,
            trades_blocked=self.trades_blocked
        )
    
    async def _on_binance_price(self, symbol: str, price_data):
        """Callback for Binance price updates."""
        if symbol != 'BTC':
            return
        
        self.latest_btc_price = quantize_price(to_decimal(price_data.price))
        self.latest_btc_timestamp = price_data.timestamp
        
        logger.debug(
            "binance_price_update",
            symbol=symbol,
            price=str(self.latest_btc_price)
        )
    
    async def _strategy_loop(self):
        """
        Main strategy loop.
        
        Polls Polymarket, compares to Binance, executes when opportunity detected.
        """
        logger.info("strategy_loop_started")
        
        while self.running:
            try:
                if self._is_paused():
                    await asyncio.sleep(to_timeout_float(self.poll_interval))
                    continue

                healthy, reason = self._evaluate_strategy_health()
                if not healthy:
                    logger.critical("strategy_health_failed", reason=reason)
                    self._pause_strategy(self.health_pause_seconds)
                    await asyncio.sleep(to_timeout_float(self.health_pause_seconds))
                    continue

                # Wait for initial Binance price
                if self.latest_btc_price is None:
                    await asyncio.sleep(to_timeout_float(Decimal("0.5")))
                    continue
                
                # Poll Polymarket odds
                await self._fetch_polymarket_odds()
                
                # Check if we have both prices
                if self.latest_polymarket_odds is None:
                    await asyncio.sleep(to_timeout_float(self.poll_interval))
                    continue
                
                # Calculate opportunity
                signal = await self._calculate_signal()
                
                if signal:
                    # Execute trade
                    await self._execute_signal(signal)
                
                # Sleep until next poll
                await asyncio.sleep(to_timeout_float(self.poll_interval))
            
            except asyncio.CancelledError:
                logger.info("strategy_loop_cancelled")
                break
            
            except Exception as e:
                logger.error(
                    "strategy_loop_error",
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
                await asyncio.sleep(to_timeout_float(self.poll_interval))
        
        logger.info("strategy_loop_stopped")
    
    async def _fetch_polymarket_odds(self):
        """Fetch current Polymarket odds for our target market."""
        try:
            # Get market data
            market_data = await self.polymarket_client.get_market(self.market_id)
            
            if not market_data:
                logger.warning(
                    "polymarket_market_not_found",
                    market_id=self.market_id
                )
                return
            
            # Extract YES/NO token prices and IDs
            yes_price = market_data.get('yes_price')
            no_price = market_data.get('no_price')
            self.yes_token_id = market_data.get('yes_token_id') or market_data.get('yes_token')
            self.no_token_id = market_data.get('no_token_id') or market_data.get('no_token')

            if not self.yes_token_id or not self.no_token_id:
                logger.warning("market_tokens_missing", market_id=self.market_id)
                return
            
            if yes_price is not None:
                self.latest_polymarket_odds = quantize_price(to_decimal(yes_price))
                self.latest_polymarket_timestamp = datetime.utcnow()

            summary = await self.polymarket_client.get_market_orderbook_summary(self.market_id)
            if summary and summary.get("ask") is not None and summary.get("bid") is not None:
                try:
                    ask = Decimal(str(summary.get("ask")))
                    bid = Decimal(str(summary.get("bid")))
                    self.latest_spread = max(Decimal("0"), ask - bid)
                except Exception:
                    self.latest_spread = None
                
                logger.debug(
                    "polymarket_odds_fetched",
                    market_id=self.market_id,
                    yes_price=str(yes_price),
                    no_price=str(no_price) if no_price is not None else None
                )
        
        except Exception as e:
            logger.error(
                "polymarket_fetch_failed",
                error=str(e),
                market_id=self.market_id
            )
    
    async def _calculate_signal(self) -> Optional[Dict]:
        """
        Calculate trading signal.
        
        Returns:
            Signal dict if opportunity exists, None otherwise
        """
        # Calculate implied probability from BTC price
        # If BTC is at $95k and target is $100k, probability should be ~95%
        implied_probability = min(
            Decimal('0.99'),
            max(Decimal('0.01'), self.latest_btc_price / self.btc_target)
        )
        
        # Calculate spread
        # Positive spread = Polymarket underpricing (BUY opportunity)
        # Negative spread = Polymarket overpricing (SELL opportunity)
        spread = implied_probability - self.latest_polymarket_odds
        spread_bps = spread * Decimal('10000')
        
        logger.debug(
            "signal_calculation",
            btc_price=str(self.latest_btc_price),
            implied_prob=str(implied_probability),
            polymarket_odds=str(self.latest_polymarket_odds),
            spread_bps=str(spread_bps)
        )
        
        # Check if spread exceeds threshold
        if abs(spread_bps) < self.min_spread_bps:
            return None
        
        # Sanity check: spread too large = data error
        if abs(spread_bps) > self.max_spread_bps:
            logger.warning(
                "spread_too_large",
                spread_bps=str(spread_bps),
                max_spread_bps=str(self.max_spread_bps),
                action="skipping"
            )
            return None
        
        # Determine action
        if spread_bps > 0:
            # Polymarket underpriced -> BUY YES
            action = 'BUY_YES'
            side = 'YES'
            target_price = quantize_price(
                self.latest_polymarket_odds + (spread / Decimal('2'))
            )  # Mid spread
        else:
            # Polymarket overpriced -> BUY NO
            action = 'BUY_NO'
            side = 'NO'
            target_price = quantize_price(
                self.latest_polymarket_odds - (abs(spread) / Decimal('2'))
            )
        
        self.signals_generated += 1
        
        confidence = min(Decimal("1"), (abs(spread_bps) / Decimal("100")))

        signal = {
            'action': action,
            'side': side,
            'spread_bps': spread_bps,
            'target_price': target_price,
            'implied_probability': implied_probability,
            'polymarket_odds': self.latest_polymarket_odds,
            'btc_price': self.latest_btc_price,
            'confidence': confidence,  # 1 bps = 1% confidence
            'correlation_id': generate_correlation_id()
        }
        
        logger.info(
            "signal_generated",
            action=action,
            side=side,
            spread_bps=str(spread_bps),
            confidence=str(signal['confidence'])
        )
        
        return signal
    
    async def _execute_signal(self, signal: Dict):
        """
        Execute trading signal.
        
        Args:
            signal: Signal dictionary
        """
        try:
            # Get current equity
            equity = await self.ledger.get_equity()
            
            # Check circuit breaker
            can_trade = await self.circuit_breaker.can_trade(equity)
            
            if not can_trade:
                logger.warning(
                    "trade_blocked_by_circuit_breaker",
                    state=self.circuit_breaker.state.value,
                    reason="Circuit breaker OPEN"
                )
                self.trades_blocked += 1
                return
            
            # Calculate position size
            max_position_value = equity * (self.max_position_pct / Decimal('100'))
            target_price = signal['target_price']
            quantity = quantize_quantity(max_position_value / target_price)
            
            if quantity <= 0:
                logger.warning("quantity_too_small", quantity=str(quantity))
                return

            spread = self.latest_spread or Decimal("0")
            breakeven = self._calculate_breakeven_with_costs(target_price, quantity, spread)
            min_target_price = breakeven * (Decimal("1") + self.min_profit_buffer_pct)
            expected_price = signal["implied_probability"] if signal["side"] == "YES" else (Decimal("1") - signal["implied_probability"])
            if expected_price < min_target_price:
                logger.debug(
                    "skipping_below_breakeven",
                    expected_price=str(expected_price),
                    min_target_price=str(min_target_price),
                )
                return

            token_id = self.yes_token_id if signal['side'] == 'YES' else self.no_token_id
            if not token_id:
                logger.error("token_id_missing_for_trade", side=signal['side'])
                return

            logger.info(
                "executing_trade",
                action=signal['action'],
                side=signal['side'],
                quantity=str(quantity),
                price=str(target_price),
                position_value=str(quantity * target_price),
                max_allowed=str(max_position_value)
            )
            
            correlation_id = signal.get("correlation_id") if isinstance(signal, dict) else None

            # Place order
            with CorrelationContext.use(correlation_id):
                result = await self.execution.place_order(
                    strategy="latency_arbitrage",
                    market_id=self.market_id,
                    token_id=token_id,
                    side="BUY",
                    quantity=quantity,
                    price=target_price,
                    metadata={
                        'outcome': signal['side'],
                        'spread_bps': str(signal['spread_bps']),
                        'btc_price': str(signal['btc_price']),
                        'implied_prob': str(signal['implied_probability']),
                        'polymarket_odds': str(signal['polymarket_odds']),
                        'confidence': str(signal['confidence']),
                        'correlation_id': correlation_id,
                    },
                    correlation_id=correlation_id,
                    idempotency_key=signal.get('idempotency_key') if isinstance(signal, dict) else None
                )
            
            if result.success:
                self.trades_executed += 1
                
                logger.info(
                    "trade_executed_successfully",
                    order_id=result.order_id,
                    filled_quantity=str(result.filled_quantity),
                    filled_price=str(result.filled_price),
                    fees=str(result.fees),
                    execution_time_ms=result.execution_time_ms
                )
                
                # Update circuit breaker (simulate small profit)
                new_equity = await self.ledger.get_equity()
                pnl = signal['spread_bps'] / Decimal('10000') * quantity * target_price
                await self.circuit_breaker.record_trade_result(new_equity, pnl)
            
            else:
                logger.error(
                    "trade_execution_failed",
                    error=result.error
                )
                self.trades_blocked += 1

            if result.get("is_duplicate"):
                logger.warning(
                    "duplicate_averted",
                    order_id=result.order_id,
                    idempotency_key=result.get("idempotency_key"),
                    correlation_id=result.get("correlation_id")
                )
        
        except Exception as e:
            logger.error(
                "signal_execution_error",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            self.trades_blocked += 1
    
    def record_trade_outcome(self, win: bool, roi: Decimal) -> None:
        self.strategy_health.record_trade(win=win, roi=roi)

    def _evaluate_strategy_health(self) -> tuple[bool, str]:
        return self.strategy_health.check_health()

    def _pause_strategy(self, duration_seconds: int) -> None:
        self._paused_until = datetime.utcnow() + timedelta(seconds=duration_seconds)

    def _is_paused(self) -> bool:
        if not self._paused_until:
            return False
        if datetime.utcnow() >= self._paused_until:
            self._paused_until = None
            return False
        return True

    def _calculate_breakeven_with_costs(
        self,
        entry_price: Decimal,
        quantity: Decimal,
        spread: Decimal,
    ) -> Decimal:
        spread_cost = spread / Decimal("2")
        adjusted_entry = entry_price + spread_cost
        return self.ledger.calculate_breakeven_price(adjusted_entry, quantity, self.fee_rate)

    def get_metrics(self) -> Dict:
        """Get strategy metrics."""
        return {
            'signals_generated': self.signals_generated,
            'trades_executed': self.trades_executed,
            'trades_blocked': self.trades_blocked,
            'execution_rate': (
                (Decimal(self.trades_executed) / Decimal(self.signals_generated))
                if self.signals_generated > 0 else Decimal("0")
            ),
            'latest_btc_price': str(self.latest_btc_price) if self.latest_btc_price else None,
            'latest_polymarket_odds': str(self.latest_polymarket_odds) if self.latest_polymarket_odds else None
        }
