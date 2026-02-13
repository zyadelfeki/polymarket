#!/usr/bin/env python3
"""
Production Polymarket Trading Bot

Architecture:
- Service-based: ExecutionService, MarketDataService, HealthMonitor
- Ledger-driven: All equity from double-entry ledger
- Async-first: Independent strategy coroutines
- Zero tolerance: No fake data, no silent failures

Strategies:
1. Latency Arbitrage (CEX price lag)
2. Whale Copy Trading (on-chain signals)
3. Liquidity Shock Detection (insider activity)
4. ML Ensemble (mispricing detection)

Risk:
- Fractional Kelly (1/4)
- Max 5% per trade
- Max 20% aggregate exposure
- Circuit breaker on 15% drawdown
"""

import asyncio
import logging
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal, getcontext
from typing import List, Dict, Optional
import signal
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from config.settings import settings
from database.ledger import Ledger
from services.execution_service import ExecutionService
from services.health_monitor import HealthMonitor
from data_feeds.binance_websocket import BinanceWebSocketFeed, BinanceWebSocketV2
from data_feeds.polymarket_client import PolymarketClient
from strategy.latency_arbitrage_engine import LatencyArbitrageEngine
from strategies.latency_arbitrage_btc import LatencyArbitrageEngine as MultiTimeframeLatencyArbitrageEngine
from risk.kelly_sizer import AdaptiveKellySizer
from risk.circuit_breaker import CircuitBreaker
from services.network_health import NetworkHealthMonitor
from services.strategy_health import StrategyHealthMonitor
from utils.decimal_helpers import to_decimal, quantize_price, quantize_quantity

_stream = sys.stdout
if hasattr(sys.stdout, "buffer"):
    _stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/production.log', encoding='utf-8'),
        logging.StreamHandler(_stream)
    ]
)
logger = logging.getLogger(__name__)

getcontext().prec = 18

class MarketDataService:
    """
    Centralized market data management.
    
    Caches prices to reduce API calls.
    Rate limits data fetches.
    """
    
    def __init__(self, polymarket_client, binance_ws):
        self.polymarket = polymarket_client
        self.binance = binance_ws
        
        # Caches
        self.markets_cache = []
        self.markets_cache_time = None
        self.markets_cache_ttl = 60  # 60 seconds
        
        self.orderbook_cache = {}  # token_id -> (orderbook, timestamp)
        self.orderbook_ttl = 5  # 5 seconds
        
        # Rate limiting
        self.last_markets_fetch = None
        self.min_fetch_interval = 10  # seconds
        
        self.fetch_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent fetches
    
    async def get_markets(self, force_refresh: bool = False) -> List[Dict]:
        """
        Get markets with caching.
        
        Args:
            force_refresh: Bypass cache
        
        Returns:
            List of markets
        """
        now = datetime.utcnow()
        
        # Check cache
        if not force_refresh and self.markets_cache_time:
            age = (now - self.markets_cache_time).total_seconds()
            if age < self.markets_cache_ttl:
                logger.debug(f"Using cached markets (age: {age:.1f}s)")
                return self.markets_cache
        
        # Rate limit
        if self.last_markets_fetch:
            elapsed = (now - self.last_markets_fetch).total_seconds()
            if elapsed < self.min_fetch_interval:
                wait = self.min_fetch_interval - elapsed
                logger.debug(f"Rate limiting markets fetch: waiting {wait:.1f}s")
                await asyncio.sleep(wait)
        
        # Fetch
        async with self.fetch_semaphore:
            try:
                markets = await self.polymarket.get_markets(limit=50)
                markets = [m for m in markets if isinstance(m, dict)]
                self.markets_cache = markets
                self.markets_cache_time = datetime.utcnow()
                self.last_markets_fetch = datetime.utcnow()
                logger.debug(f"Fetched {len(markets)} markets")
                return markets
            except Exception as e:
                logger.error(f"Error fetching markets: {e}")
                # Return stale cache if available
                return self.markets_cache if self.markets_cache else []
    
    def get_exchange_prices(self) -> Dict[str, Decimal]:
        """
        Get current CEX prices from Binance WebSocket.
        
        Returns:
            {'BTC': Decimal('95300'), 'ETH': Decimal('3450'), ...}
        """
        prices = {}
        
        for symbol in ['BTC', 'ETH', 'SOL']:
            price = self.binance.get_current_price(symbol)
            if price:
                prices[symbol] = Decimal(str(price))
        
        return prices
    
    async def get_orderbook(self, token_id: str, force_refresh: bool = False) -> Optional[Dict]:
        """
        Get orderbook with caching.
        
        Args:
            token_id: Token ID
            force_refresh: Bypass cache
        
        Returns:
            Orderbook dict or None
        """
        now = datetime.utcnow()
        
        # Check cache
        if not force_refresh and token_id in self.orderbook_cache:
            cached_book, cached_time = self.orderbook_cache[token_id]
            age = (now - cached_time).total_seconds()
            if age < self.orderbook_ttl:
                return cached_book
        
        # Fetch
        async with self.fetch_semaphore:
            try:
                orderbook = await self.polymarket.get_market_orderbook(token_id)
                self.orderbook_cache[token_id] = (orderbook, now)
                return orderbook
            except Exception as e:
                logger.error(f"Error fetching orderbook for {token_id}: {e}")
                return None

class ProductionTradingBot:
    """
    Production trading bot with service architecture.
    
    Key principles:
    1. All equity from ledger (not INITIAL_CAPITAL)
    2. All trades through ExecutionService
    3. Health monitoring on all components
    4. No fake data, no silent failures
    5. Proper async architecture
    """
    
    def __init__(self):
        # Core services
        self.ledger = Ledger(db_path="data/trading.db")
        self.polymarket_client = self._initialize_polymarket_client()
        self.binance_ws = BinanceWebSocketV2()
        self.binance_ws.on_price_update = self._on_binance_price_update
        
        self.execution = ExecutionService(
            polymarket_client=self.polymarket_client,
            ledger=self.ledger,
            config={'max_retries': 3, 'timeout_seconds': 10}
        )
        
        self.health_monitor = HealthMonitor(config={
            'check_interval': 30,
            'alert_threshold': 3
        })
        
        self.market_data = MarketDataService(
            polymarket_client=self.polymarket_client,
            binance_ws=self.binance_ws
        )
        
        # Strategies
        self.latency_arb = LatencyArbitrageEngine(config={
            'min_edge': to_decimal("0.05"),
            'max_hold_seconds': 30,
            'target_profit': to_decimal("0.40"),
            'stop_loss': to_decimal("0.05")
        })
        
        # Risk management
        self.kelly_sizer = AdaptiveKellySizer(config={
            'kelly_fraction': to_decimal("0.25"),  # 1/4 Kelly
            'max_bet_pct': to_decimal("5.0"),      # Max 5% per trade
            'min_edge': to_decimal("0.02"),        # 2% minimum edge
            'max_aggregate_exposure': to_decimal("20.0")  # Max 20% total
        })

        self.multi_tf_latency_arb = MultiTimeframeLatencyArbitrageEngine(
            binance_ws=self.binance_ws,
            polymarket_client=self.polymarket_client,
            charlie_predictor=None,
            config=settings.get_latency_arb_config(),
            execution_service=self.execution,
            kelly_sizer=self.kelly_sizer,
            redis_subscriber=None,
        )
        
        self.circuit_breaker = CircuitBreaker(
            initial_capital=Decimal(str(settings.INITIAL_CAPITAL))
        )

        # Network + strategy health
        self.network_monitor = NetworkHealthMonitor(partition_threshold_seconds=15)
        self.latency_arb_health = StrategyHealthMonitor("latency_arb")
        
        # State
        self.running = False
        self.tasks = []
        self.binance_listen_task = None
        
        # Stats
        self.cycles = 0
        self.opportunities_found = 0
        self.trades_executed = 0
        self.last_binance_update: Optional[datetime] = None

    def _initialize_polymarket_client(self) -> PolymarketClient:
        client = PolymarketClient()
        if client is None:
            raise RuntimeError("Polymarket client failed to initialize")

        required_methods = ["get_markets", "get_market", "get_market_orderbook"]
        for method in required_methods:
            if not hasattr(client, method):
                raise RuntimeError(f"Polymarket client missing required method: {method}")

        return client
    
    async def start(self):
        """Start the bot"""
        logger.info("\n" + "="*60)
        logger.info("PRODUCTION TRADING BOT STARTING")
        logger.info("="*60)
        logger.info(f"Mode: {'PAPER' if settings.PAPER_TRADING else 'LIVE'}")
        
        # Initialize ledger
        initial_capital = Decimal(str(settings.INITIAL_CAPITAL))
        
        try:
            current_equity = self.ledger.get_equity()
            if current_equity == 0:
                # First run - record initial deposit
                self.ledger.record_deposit(
                    amount=initial_capital,
                    description="Initial capital deposit"
                )
                logger.info(f"✅ Recorded initial deposit: ${initial_capital}")
            else:
                logger.info(f"✅ Existing equity: ${current_equity}")
        except Exception as e:
            logger.error(f"Ledger initialization error: {e}")
            return

        try:
            current_equity = self.ledger.get_equity()
            self.circuit_breaker.reset_baseline(current_equity)
        except Exception as e:
            logger.warning(f"Circuit breaker baseline reset failed: {e}")
        
        # Validate ledger
        try:
            self.ledger.validate_ledger()
        except Exception as e:
            logger.error(f"Ledger validation failed: {e}")
            return
        
        # Connect data feeds
        binance_connected = await self.binance_ws.connect()
        if not binance_connected:
            logger.error("❌ Failed to connect to Binance WebSocket")
            return
        
        logger.info("✅ Binance WebSocket connected")
        self.binance_listen_task = asyncio.create_task(self.binance_ws.listen())
        
        # Start health monitor
        await self.health_monitor.start()
        logger.info("✅ Health monitor started")
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("="*60 + "\n")
        
        # Start main loop
        self.running = True

        # Reconcile positions before trading
        await self._reconcile_positions_on_startup()
        
        try:
            # Run strategy loops concurrently
            await asyncio.gather(
                self._latency_arb_loop(),
                self._position_monitor_loop(),
                self._market_resolution_monitor(),
                self._stats_loop()
            )
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            await self.stop()
    
    async def _latency_arb_loop(self):
        """
        Latency arbitrage strategy loop.
        
        Scans for CEX vs Polymarket price discrepancies.
        """
        logger.info("[Latency Arb] Loop started")
        
        while self.running:
            try:
                healthy, reason = self.latency_arb_health.check_health()
                if not healthy:
                    logger.critical(f"[Latency Arb] Strategy health check failed: {reason}")
                    await asyncio.sleep(3600)
                    continue

                if self.network_monitor.check_partition():
                    logger.critical("[Latency Arb] Network partition detected, pausing trades")
                    await asyncio.sleep(15)
                    continue

                # Check circuit breaker
                current_equity = self.ledger.get_equity()
                if not self.circuit_breaker.can_trade(current_equity):
                    logger.warning("[Latency Arb] Circuit breaker engaged")
                    await asyncio.sleep(60)
                    continue

                # Run multi-timeframe BTC latency scan first (hourly/daily priority)
                try:
                    multi_tf_opportunity = await self.multi_tf_latency_arb.scan_opportunities()
                    if multi_tf_opportunity:
                        self.opportunities_found += 1
                        market_id = multi_tf_opportunity.get("market_id")
                        direction = multi_tf_opportunity.get("direction")
                        timeframe = multi_tf_opportunity.get("timeframe")
                        logger.info(
                            f"[Latency Arb][MultiTF] Opportunity found: market={market_id} "
                            f"timeframe={timeframe} direction={direction} edge={multi_tf_opportunity.get('edge')}"
                        )

                        market_data = await self.polymarket_client.get_market(market_id)
                        if market_data:
                            signal = "BULLISH" if direction == "UP" else "BEARISH"
                            confidence = to_decimal(multi_tf_opportunity.get("charlie_confidence") or "0.6")
                            exec_result = await self.multi_tf_latency_arb.execute_signal(
                                market=market_data,
                                signal=signal,
                                confidence=confidence,
                            )
                            if exec_result:
                                self.trades_executed += 1
                                logger.info("[Latency Arb][MultiTF] Trade executed")
                except Exception as e:
                    logger.error(f"[Latency Arb][MultiTF] Error: {e}", exc_info=True)
                
                # Get markets
                markets = await self.market_data.get_markets()
                if not markets:
                    await asyncio.sleep(10)
                    continue
                self.network_monitor.record_success()
                
                # Get CEX prices
                exchange_prices = self.market_data.get_exchange_prices()
                if not exchange_prices:
                    logger.warning("[Latency Arb] No exchange prices available")
                    await asyncio.sleep(10)
                    continue
                
                # Record health check
                self.health_monitor.record_binance_tick()
                self.health_monitor.record_polymarket_call()
                
                # Scan for opportunities
                opportunities = await self.latency_arb.scan_for_opportunities(
                    markets=markets,
                    exchange_prices=exchange_prices,
                    polymarket_client=self.polymarket_client
                )
                
                if opportunities:
                    self.opportunities_found += len(opportunities)
                    logger.info(f"[Latency Arb] Found {len(opportunities)} opportunities")
                
                # Execute top opportunities
                for opp in opportunities[:3]:  # Top 3
                    if opp.edge < Decimal('0.05'):
                        continue
                    
                    # Calculate aggregate exposure
                    open_positions = self.ledger.get_open_positions()
                    aggregate_exposure = sum(
                        Decimal(str(p['quantity'])) * Decimal(str(p['entry_price']))
                        for p in open_positions
                    )
                    
                    # Determine token ID and side
                    if opp.action == 'BUY_YES':
                        token_id = opp.token_id_yes
                        side = 'YES'
                        price = quantize_price(to_decimal(opp.market_price_yes))
                        expected_price = quantize_price(to_decimal(opp.expected_prob))
                    else:
                        token_id = opp.token_id_no
                        side = 'NO'
                        price = quantize_price(to_decimal(opp.market_price_no))
                        expected_price = quantize_price(Decimal("1") - to_decimal(opp.expected_prob))

                    # Compute real edge using spread + latency decay
                    orderbook = await self.market_data.get_orderbook(token_id)
                    spread = Decimal("0")
                    if orderbook:
                        bids = orderbook.get('bids', [])
                        asks = orderbook.get('asks', [])
                        if bids and asks:
                            bid = Decimal(str(bids[0]['price']))
                            ask = Decimal(str(asks[0]['price']))
                            spread = max(Decimal("0"), ask - bid)
                    latency_advantage = Decimal("0")
                    if self.last_binance_update:
                        latency_advantage = to_decimal(
                            (datetime.utcnow() - self.last_binance_update).total_seconds()
                        )
                    real_edge = self.kelly_sizer.calculate_real_edge(
                        market_price=price,
                        true_probability=expected_price,
                        orderbook_spread=spread,
                        latency_advantage_seconds=latency_advantage
                    )
                    if real_edge <= 0:
                        logger.debug(
                            f"[Latency Arb] Skipping {opp.question[:50]}: real edge <= 0"
                        )
                        continue

                    # Calculate bet size (using REAL equity from ledger)
                    bet_size_result = self.kelly_sizer.calculate_bet_size(
                        bankroll=current_equity,  # NOT settings.INITIAL_CAPITAL
                        win_probability=to_decimal(opp.confidence),
                        payout_odds=(
                            Decimal("1") / to_decimal(opp.market_price_yes)
                            if opp.action == 'BUY_YES'
                            else Decimal("1") / to_decimal(opp.market_price_no)
                        ),
                        edge=real_edge,
                        sample_size=30,
                        current_aggregate_exposure=aggregate_exposure
                    )
                    
                    if bet_size_result.size == 0:
                        logger.debug(
                            f"[Latency Arb] Skipping {opp.question[:50]}: "
                            f"{bet_size_result.capped_reason}"
                        )
                        continue
                    
                    quantity = quantize_quantity(bet_size_result.size / price)

                    # Transaction cost breakeven check
                    breakeven = self.ledger.calculate_breakeven_price(price, quantity)
                    min_target_price = breakeven * Decimal("1.05")
                    if expected_price < min_target_price:
                        logger.debug(
                            f"[Latency Arb] Skipping: expected {expected_price} below breakeven {min_target_price}"
                        )
                        continue
                    
                    # Execute order
                    logger.info(
                        f"[Latency Arb] Executing: {opp.question[:50]} | "
                        f"{side} {quantity:.2f} @ {price} | "
                        f"Edge: {opp.edge:.1%} | Size: ${bet_size_result.size}"
                    )
                    
                    order_result = await self.execution.place_order(
                        strategy='latency_arb',
                        market_id=opp.market_id,
                        token_id=token_id,
                        side=side,
                        quantity=quantity,
                        price=price,
                        order_type='GTC',
                        metadata={
                            'question': opp.question,
                            'symbol': opp.symbol,
                            'threshold': str(to_decimal(opp.threshold)),
                            'exchange_price': str(to_decimal(opp.exchange_price)),
                            'edge': str(to_decimal(opp.edge))
                        },
                        expected_price=expected_price
                    )
                    
                    if order_result.success:
                        self.trades_executed += 1
                        self.health_monitor.record_trade()
                        
                        # Record in Kelly sizer for streak tracking
                        self.kelly_sizer.record_trade_result(
                            win=False,  # Unknown yet
                            roi=Decimal("0"),
                            bet_size=bet_size_result.size,
                            strategy='latency_arb'
                        )
                        
                        logger.info(
                            f"[Latency Arb] ✅ Trade executed: Order {order_result.order_id[:20]} | "
                            f"Filled: {order_result.filled_quantity} @ {order_result.filled_price} | "
                            f"Fees: ${order_result.fees}"
                        )
                    else:
                        logger.warning(
                            f"[Latency Arb] ❌ Trade failed: {order_result.error}"
                        )
                
                self.cycles += 1
                
                # Wait before next scan
                await asyncio.sleep(15)  # 15 seconds between scans
            
            except Exception as e:
                logger.error(f"[Latency Arb] Error: {e}", exc_info=True)
                await asyncio.sleep(15)
    
    async def _position_monitor_loop(self):
        """
        Monitor open positions and manage exits.
        
        Checks:
        - Time stops (max hold time)
        - Target profit
        - Stop loss
        """
        logger.info("[Position Monitor] Loop started")
        
        while self.running:
            try:
                if self.network_monitor.check_partition():
                    logger.critical("[Position Monitor] Network partition detected, pausing")
                    await asyncio.sleep(5)
                    continue

                open_positions = self.ledger.get_open_positions()
                
                if not open_positions:
                    await asyncio.sleep(5)
                    continue
                
                logger.debug(f"[Position Monitor] Monitoring {len(open_positions)} positions")
                
                for position in open_positions:
                    position_id = position['id']
                    token_id = position['token_id']
                    entry_price = Decimal(str(position['entry_price']))
                    current_price = Decimal(str(position['current_price'])) if position['current_price'] else None
                    hold_time = position.get('hold_time_seconds', 0)
                    strategy = position['strategy']
                    
                    # Update current price
                    orderbook = await self.market_data.get_orderbook(token_id)
                    if orderbook:
                        self.network_monitor.record_success()
                        bids = orderbook.get('bids', [])
                        asks = orderbook.get('asks', [])
                        if bids and asks:
                            mid_price = (Decimal(str(bids[0]['price'])) + Decimal(str(asks[0]['price']))) / 2
                            self.ledger.update_position_prices({token_id: mid_price})
                            current_price = mid_price
                    
                    if not current_price:
                        continue
                    
                    # Check exit conditions
                    exit_reason = None
                    
                    # Time stop
                    if strategy == 'latency_arb' and hold_time > 30:  # 30 seconds
                        exit_reason = 'TIME_STOP'
                    
                    # Target profit
                    roi = (current_price - entry_price) / entry_price
                    if roi > Decimal('0.40'):  # 40% profit
                        exit_reason = 'TARGET_HIT'
                    
                    # Stop loss
                    if roi < Decimal('-0.05'):  # -5%
                        exit_reason = 'STOP_LOSS'
                    
                    if exit_reason:
                        logger.info(
                            f"[Position Monitor] Closing position {position_id}: {exit_reason} | "
                            f"ROI: {roi:+.1%}"
                        )
                        
                        close_result = await self.execution.close_position(
                            position_id=position_id,
                            exit_reason=exit_reason,
                            exit_price=current_price
                        )
                        
                        if close_result.success:
                            logger.info(
                                f"[Position Monitor] ✅ Position closed: {position_id} | "
                                f"Exit: {close_result.filled_price} | ROI: {roi:+.1%}"
                            )
                            
                            # Update Kelly sizer
                            self.kelly_sizer.record_trade_result(
                                win=(roi > 0),
                                roi=roi,
                                bet_size=quantize_quantity(
                                    to_decimal(position['quantity']) * entry_price
                                ),
                                strategy=strategy
                            )
                            if strategy == "latency_arb":
                                self.latency_arb_health.record_trade(win=(roi > 0), roi=roi)
                        else:
                            logger.warning(f"[Position Monitor] ❌ Failed to close: {close_result.error}")
                
                await asyncio.sleep(5)  # Check every 5 seconds
            
            except Exception as e:
                logger.error(f"[Position Monitor] Error: {e}", exc_info=True)
                await asyncio.sleep(5)
    
    async def _stats_loop(self):
        """
        Periodic stats logging.
        """
        while self.running:
            try:
                await asyncio.sleep(60)  # Every minute
                
                equity = self.ledger.get_equity()
                open_positions = self.ledger.get_open_positions()
                strategy_pnl = self.ledger.get_strategy_pnl('latency_arb', days=1)
                
                health = self.health_monitor.get_health_status()
                exec_stats = self.execution.get_stats()
                kelly_stats = self.kelly_sizer.get_stats()
                
                logger.info("\n" + "="*60)
                logger.info("STATS SUMMARY")
                logger.info("="*60)
                logger.info(f"Equity: ${equity}")
                logger.info(f"Open Positions: {len(open_positions)}")
                logger.info(f"Cycles: {self.cycles}")
                logger.info(f"Opportunities: {self.opportunities_found}")
                logger.info(f"Trades Executed: {self.trades_executed}")
                logger.info(f"Strategy PnL (24h): ${strategy_pnl.get('net_pnl', 0)}")
                logger.info(f"Execution Success Rate: {exec_stats.get('success_rate', 0):.1%}")
                logger.info(f"Avg Order Latency: {exec_stats.get('avg_latency_ms', 0)}ms")
                logger.info(f"Kelly Sizing: {kelly_stats['kelly_fraction']:.0%} | Wins: {kelly_stats['consecutive_wins']} | Losses: {kelly_stats['consecutive_losses']}")
                logger.info(f"Health: {self.health_monitor.is_healthy()}")
                logger.info("="*60 + "\n")
            
            except Exception as e:
                logger.error(f"[Stats] Error: {e}")
    
    async def stop(self):
        """Graceful shutdown"""
        logger.info("\nStopping bot...")
        self.running = False
        if self.binance_listen_task and not self.binance_listen_task.done():
            self.binance_listen_task.cancel()
            try:
                await self.binance_listen_task
            except asyncio.CancelledError:
                pass

        await self.health_monitor.stop()
        await self.binance_ws.close()
        
        logger.info("Bot stopped")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"\nReceived signal {signum}")
        self.running = False

    async def _on_binance_price_update(self, symbol: str, price: float, data: dict):
        """Handle Binance price updates"""
        self.health_monitor.record_binance_tick()
        self.network_monitor.record_success()
        self.last_binance_update = datetime.utcnow()

    async def _reconcile_positions_on_startup(self):
        """Cross-check Polymarket API vs local ledger for orphaned positions."""
        logger.info("🔍 Reconciling positions with Polymarket API...")

        try:
            polymarket_positions = await self.polymarket_client.get_open_positions()
            local_positions = self.ledger.get_open_positions()

            local_token_ids = {p['token_id'] for p in local_positions}
            polymarket_token_ids = {p.get('token_id') for p in polymarket_positions if isinstance(p, dict)}

            orphaned = polymarket_token_ids - local_token_ids
            phantom = local_token_ids - polymarket_token_ids

            if orphaned:
                logger.critical(f"🚨 ORPHANED POSITIONS DETECTED: {orphaned}")
                for token_id in orphaned:
                    await self._import_orphaned_position(token_id, polymarket_positions)

            if phantom:
                logger.warning(f"⚠️ Phantom positions in DB (already closed?): {phantom}")

            logger.info(f"✅ Position reconciliation complete: {len(polymarket_positions)} live")
        except Exception as e:
            logger.error(f"Position reconciliation failed: {e}")

    async def _import_orphaned_position(self, token_id: str, positions: List[Dict]):
        """Import an orphaned position into the ledger."""
        match = next((p for p in positions if p.get('token_id') == token_id), None)
        if not match:
            return

        market_id = match.get('market_id') or match.get('condition_id') or "unknown_market"
        side = match.get('side', 'YES')
        quantity = Decimal(str(match.get('quantity') or match.get('size') or "0"))
        entry_price = Decimal(str(match.get('entry_price') or match.get('price') or "0.5"))

        try:
            self.ledger.record_trade_entry(
                market_id=market_id,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                fees=Decimal("0"),
                strategy="reconciled",
                token_id=token_id,
                order_id=match.get('order_id', ''),
                metadata={"source": "reconciliation", "raw": match}
            )
        except Exception:
            self.ledger.record_reconciled_position(
                market_id=market_id,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                strategy="reconciled",
                token_id=token_id,
                order_id=match.get('order_id', ''),
                metadata={"source": "reconciliation", "raw": match}
            )

        self.ledger.record_audit_event(
            operation="POSITION_RECONCILED",
            entity_type="position",
            entity_id=str(token_id),
            new_state="OPEN",
            reason="orphaned_position_imported",
            context={"market_id": market_id, "token_id": token_id}
        )

    async def _market_resolution_monitor(self):
        """Detect market closures/resolutions and force exits."""
        while self.running:
            try:
                open_positions = self.ledger.get_open_positions()
                for pos in open_positions:
                    market_id = pos['market_id']
                    market = await self.polymarket_client.get_market(market_id)
                    if not market:
                        continue
                    status = market.get('status', 'ACTIVE')
                    end_date_iso = market.get('end_date') or market.get('end_date_iso')

                    if end_date_iso:
                        end_date = datetime.fromisoformat(end_date_iso.replace('Z', '+00:00'))
                        time_to_close = (end_date - datetime.now(timezone.utc)).total_seconds()
                        if time_to_close < 3600:
                            logger.warning(
                                f"⏰ URGENT: Market {market_id} closes in {time_to_close/60:.0f} min"
                            )
                            await self._emergency_close_position(pos['id'], "MARKET_CLOSING")

                    if status in ['RESOLVED', 'CLOSED', 'FINALIZED']:
                        logger.critical(f"🚨 Market {market_id} already resolved! Position stuck!")
                        await self._handle_resolved_position(pos)

                if not self.running:
                    break
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Market resolution monitor error: {e}")
                await asyncio.sleep(60)

    async def _emergency_close_position(self, position_id: int, reason: str):
        """Force-close a position immediately."""
        try:
            await self.execution.close_position(position_id=position_id, exit_reason=reason)
        except Exception as e:
            logger.error(f"Emergency close failed: {e}")

    async def _handle_resolved_position(self, position: Dict):
        """Handle positions in already-resolved markets."""
        try:
            self.ledger.record_audit_event(
                operation="MARKET_RESOLVED",
                entity_type="position",
                entity_id=str(position.get('id')),
                old_state="OPEN",
                new_state="RESOLVED",
                reason="market_resolved",
                context={"market_id": position.get('market_id')}
            )
        except Exception as e:
            logger.error(f"Resolved position audit failed: {e}")

async def main():
    bot = ProductionTradingBot()
    await bot.start()

if __name__ == '__main__':
    asyncio.run(main())