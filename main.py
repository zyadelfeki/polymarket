#!/usr/bin/env python3
"""
Polymarket Volatility Arbitrage Bot V2.0
Production orchestrator
"""
import asyncio
import signal
import sys
from decimal import Decimal
import logging

from config.settings import settings
from utils.logger import setup_logging
from utils.db import Database

from data_feeds.binance_websocket import BinanceWebSocketFeed
from data_feeds.news_scanner import NewsScanner
from data_feeds.polymarket_client import PolymarketClient

from intelligence.sentiment_scorer import SentimentScorer
from intelligence.prediction_engine import PredictionEngine

from strategy.volatility_arbitrage import VolatilityArbitrageEngine

from risk.adaptive_kelly import AdaptiveKellySizer
from risk.circuit_breaker import CircuitBreaker
from risk.position_manager import PositionManager
from risk.bankroll_tracker import BankrollTracker

logger = logging.getLogger(__name__)

class PolymarketBot:
    """Main bot orchestrator"""
    
    def __init__(self):
        # Validate config
        if not settings.validate():
            logger.critical("❌ Configuration validation failed")
            sys.exit(1)
        
        settings.print_config()
        
        # Initialize database
        self.db = Database(settings.DATABASE_PATH)
        
        # Initialize components
        self.binance_feed = BinanceWebSocketFeed()
        self.news_scanner = NewsScanner()
        self.polymarket = PolymarketClient()
        
        self.sentiment = SentimentScorer()
        self.prediction_engine = PredictionEngine(
            self.news_scanner,
            self.sentiment,
            self.binance_feed
        )
        
        # Risk management
        self.bankroll = BankrollTracker(settings.INITIAL_CAPITAL)
        self.kelly = AdaptiveKellySizer(settings.INITIAL_CAPITAL)
        self.circuit_breaker = CircuitBreaker()
        self.position_manager = PositionManager(settings.MAX_OPEN_POSITIONS)
        
        # Strategy engines
        self.volatility_arb = VolatilityArbitrageEngine(
            self.binance_feed,
            self.polymarket,
            self.bankroll
        )
        
        # Connect callbacks
        self.binance_feed.on_volatility_spike = self.volatility_arb.on_volatility_spike
        
        self.running = False
    
    async def start(self):
        """Start all bot components"""
        logger.info("⚡ Starting bot...")
        self.running = True
        
        # Start background tasks
        tasks = [
            asyncio.create_task(self.binance_feed.listen()),
            asyncio.create_task(self.news_monitoring_loop()),
            asyncio.create_task(self.trading_loop()),
            asyncio.create_task(self.volatility_arb.monitor_positions()),
            asyncio.create_task(self.performance_monitoring_loop())
        ]
        
        logger.info("✅ Bot running - Press Ctrl+C to stop")
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("⚠️ Tasks cancelled")
    
    async def news_monitoring_loop(self):
        """Background news scanning"""
        while self.running:
            try:
                news = await self.news_scanner.scan()
                if news:
                    logger.debug(f"📰 Scanned {len(news)} news items")
            except Exception as e:
                logger.error(f"News scan error: {e}")
            
            await asyncio.sleep(settings.NEWS_SCAN_INTERVAL)
    
    async def trading_loop(self):
        """Main trading logic"""
        while self.running:
            try:
                # Check circuit breaker
                breaker_status = self.circuit_breaker.check(
                    self.bankroll.current_capital,
                    self.bankroll.initial_capital
                )
                
                if not breaker_status["can_trade"]:
                    logger.critical(f"⛔ Trading halted: {breaker_status['reason']}")
                    await asyncio.sleep(60)
                    continue
                
                # Check if we can open positions
                if not self.position_manager.can_open_position():
                    await asyncio.sleep(10)
                    continue
                
                # Scan markets
                markets = await self.polymarket.scan_markets_parallel(["BTC", "ETH", "SOL"])
                
                if not markets:
                    await asyncio.sleep(settings.MARKET_SCAN_INTERVAL)
                    continue
                
                # Analyze opportunities
                opportunities = await self.prediction_engine.scan_all_opportunities(markets)
                
                if opportunities:
                    logger.info(f"✅ Found {len(opportunities)} opportunities")
                    
                    # Execute best opportunity
                    await self.execute_trade(opportunities[0])
                
            except Exception as e:
                logger.error(f"Trading loop error: {e}")
            
            await asyncio.sleep(settings.MARKET_SCAN_INTERVAL)
    
    async def execute_trade(self, opportunity: dict):
        """Execute trading opportunity"""
        market = opportunity["market"]
        signal = opportunity["signal"]
        symbol = opportunity["symbol"]
        
        # Check if signal is strong enough
        if signal["confidence"] < settings.MIN_CONFIDENCE:
            logger.debug(f"Skipping: Low confidence {signal['confidence']:.1%}")
            return
        
        if signal["edge"] < settings.MIN_EDGE_THRESHOLD:
            logger.debug(f"Skipping: Low edge {signal['edge']:.1%}")
            return
        
        # Check liquidity
        liquidity_check = self.polymarket.check_liquidity_depth(market)
        if not liquidity_check["sufficient"]:
            logger.warning("Skipping: Insufficient liquidity")
            return
        
        # Calculate position size
        available = self.bankroll.get_available_capital()
        payout_odds = 1.0 / signal["market_odds"] if signal["market_odds"] > 0 else 2.0
        
        bet_size = self.kelly.calculate_bet_size(
            bankroll=available,
            win_probability=signal["confidence"],
            payout_odds=payout_odds,
            confidence=signal["confidence"]
        )
        
        logger.info(f"🎯 EXECUTING TRADE:")
        logger.info(f"   Market: {market['question'][:50]}...")
        logger.info(f"   Signal: {signal['signal']} @ ${signal['market_odds']:.3f}")
        logger.info(f"   Edge: {signal['edge']:.1%} | Confidence: {signal['confidence']:.1%}")
        logger.info(f"   Size: ${bet_size:.2f}")
        
        # Execute
        success = await self.polymarket.place_bet(
            market_id=market["id"],
            side=signal["side"],
            amount=float(bet_size),
            max_price=signal["market_odds"] * 1.05
        )
        
        if success:
            # Record position
            self.position_manager.add_position(
                market_id=market["id"],
                side=signal["side"],
                entry_price=signal["market_odds"],
                size=bet_size,
                confidence=signal["confidence"],
                reason=signal.get("reason", "EDGE_DETECTED")
            )
            
            # Log to database
            self.db.log_trade({
                "market_id": market["id"],
                "question": market["question"],
                "symbol": symbol,
                "side": signal["side"],
                "entry_price": signal["market_odds"],
                "size": bet_size,
                "confidence": signal["confidence"],
                "strategy": "EDGE_ARBITRAGE",
                "reason": signal.get("reason", "EDGE_DETECTED")
            })
    
    async def performance_monitoring_loop(self):
        """Monitor and log performance"""
        while self.running:
            try:
                stats = self.bankroll.get_stats()
                kelly_stats = self.kelly.get_stats()
                position_stats = self.position_manager.get_stats()
                
                logger.info("📊 PERFORMANCE UPDATE:")
                logger.info(f"   Capital: ${stats['current_capital']:.2f} ({stats['total_return_pct']:+.1f}%)")
                logger.info(f"   Trades: {stats['total_trades']} | Win Rate: {stats['win_rate_pct']:.1f}%")
                logger.info(f"   Open: {position_stats['open_count']}/{position_stats['max_positions']}")
                logger.info(f"   Kelly: {kelly_stats['kelly_multiplier']:.2f}x")
                
                # Save snapshot
                self.db.log_snapshot({**stats, **position_stats})
                
            except Exception as e:
                logger.error(f"Monitoring error: {e}")
            
            await asyncio.sleep(300)  # Every 5 minutes
    
    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("⚠️ Shutting down...")
        self.running = False
        
        await self.binance_feed.close()
        
        self.bankroll.print_summary()
        self.db.close()
        
        logger.info("👋 Bot stopped")

async def main():
    """Main entry point"""
    setup_logging(settings.LOG_LEVEL, "logs/bot.log")
    
    bot = PolymarketBot()
    
    # Handle graceful shutdown
    loop = asyncio.get_running_loop()
    
    def signal_handler():
        asyncio.create_task(bot.shutdown())
    
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Goodbye!")
        sys.exit(0)