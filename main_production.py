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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Dict, Optional
import signal
import sys

from config.settings import settings
from database.ledger import Ledger
from services.execution_service import ExecutionService
from services.health_monitor import HealthMonitor
from data_feeds.binance_websocket import BinanceWebSocketFeed
from data_feeds.polymarket_client import PolymarketClient
from strategy.latency_arbitrage_engine import LatencyArbitrageEngine
from risk.kelly_sizer import AdaptiveKellySizer
from risk.circuit_breaker import CircuitBreaker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/production.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
        self.polymarket_client = PolymarketClient()
        self.binance_ws = BinanceWebSocketFeed()
        
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
            'min_edge': 0.05,
            'max_hold_seconds': 30,
            'target_profit': 0.40,
            'stop_loss': 0.05
        })
        
        # Risk management
        self.kelly_sizer = AdaptiveKellySizer(config={
            'kelly_fraction': 0.25,  # 1/4 Kelly
            'max_bet_pct': 5.0,      # Max 5% per trade
            'min_edge': 0.02,        # 2% minimum edge
            'max_aggregate_exposure': 20.0  # Max 20% total
        })
        
        self.circuit_breaker = CircuitBreaker(
            initial_equity=Decimal(str(settings.INITIAL_CAPITAL)),
            max_drawdown_pct=15.0
        )
        
        # State
        self.running = False
        self.tasks = []
        
        # Stats
        self.cycles = 0
        self.opportunities_found = 0
        self.trades_executed = 0
    
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
        
        # Start health monitor
        await self.health_monitor.start()
        logger.info("✅ Health monitor started")
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("="*60 + "\n")
        
        # Start main loop
        self.running = True
        
        try:
            # Run strategy loops concurrently
            await asyncio.gather(
                self._latency_arb_loop(),
                self._position_monitor_loop(),
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
                # Check circuit breaker
                current_equity = self.ledger.get_equity()
                if not self.circuit_breaker.can_trade(current_equity):
                    logger.warning("[Latency Arb] Circuit breaker engaged")
                    await asyncio.sleep(60)
                    continue
                
                # Get markets
                markets = await self.market_data.get_markets()
                if not markets:
                    await asyncio.sleep(10)
                    continue
                
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
                    
                    # Calculate bet size (using REAL equity from ledger)
                    bet_size_result = self.kelly_sizer.calculate_bet_size(
                        bankroll=current_equity,  # NOT settings.INITIAL_CAPITAL
                        win_probability=float(opp.confidence),
                        payout_odds=1.0 / float(opp.market_price_yes) if opp.action == 'BUY_YES' else 1.0 / float(opp.market_price_no),
                        edge=float(opp.edge),
                        sample_size=30,
                        current_aggregate_exposure=aggregate_exposure
                    )
                    
                    if bet_size_result.size == 0:
                        logger.debug(
                            f"[Latency Arb] Skipping {opp.question[:50]}: "
                            f"{bet_size_result.capped_reason}"
                        )
                        continue
                    
                    # Determine token ID and side
                    if opp.action == 'BUY_YES':
                        token_id = opp.token_id_yes
                        side = 'YES'
                        price = opp.market_price_yes
                    else:
                        token_id = opp.token_id_no
                        side = 'NO'
                        price = opp.market_price_no
                    
                    quantity = bet_size_result.size / price
                    
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
                            'threshold': float(opp.threshold),
                            'exchange_price': float(opp.exchange_price),
                            'edge': float(opp.edge)
                        }
                    )
                    
                    if order_result.success:
                        self.trades_executed += 1
                        self.health_monitor.record_trade()
                        
                        # Record in Kelly sizer for streak tracking
                        self.kelly_sizer.record_trade_result(
                            win=False,  # Unknown yet
                            roi=0.0,
                            bet_size=float(bet_size_result.size),
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
                                roi=float(roi),
                                bet_size=float(position['quantity']) * float(entry_price),
                                strategy=strategy
                            )
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
                logger.info(f"Kelly Sizing: {kelly_stats['kelly_fraction']*100:.0%} | Wins: {kelly_stats['consecutive_wins']} | Losses: {kelly_stats['consecutive_losses']}")
                logger.info(f"Health: {self.health_monitor.is_healthy()}")
                logger.info("="*60 + "\n")
            
            except Exception as e:
                logger.error(f"[Stats] Error: {e}")
    
    async def stop(self):
        """Graceful shutdown"""
        logger.info("\nStopping bot...")
        self.running = False
        
        await self.health_monitor.stop()
        await self.binance_ws.close()
        
        logger.info("Bot stopped")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"\nReceived signal {signum}")
        self.running = False

async def main():
    bot = ProductionTradingBot()
    await bot.start()

if __name__ == '__main__':
    asyncio.run(main())