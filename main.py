#!/usr/bin/env python3
import asyncio
import signal
import sys
from datetime import datetime
import logging
from pathlib import Path

from config.settings import settings
from utils.logger import setup_logger
from utils.db import Database
from data_feeds.binance_websocket import BinanceWebSocketFeed
from data_feeds.news_scanner import NewsScanner
from data_feeds.polymarket_client import PolymarketCLOBClient
from intelligence.sentiment_scorer import SentimentScorer
from strategy.volatility_arbitrage import VolatilityArbitrageEngine
from strategy.threshold_arbitrage import ThresholdArbitrageEngine
from risk.bankroll_tracker import BankrollTracker
from risk.adaptive_kelly import AdaptiveKellySizer
from risk.circuit_breaker import CircuitBreaker

logger = setup_logger("main", settings.LOG_LEVEL)

class PolymarketBot:
    def __init__(self):
        self.running = False
        self.db = Database()
        self.bankroll = BankrollTracker(self.db)
        self.circuit_breaker = CircuitBreaker(self.bankroll)
        self.kelly_sizer = AdaptiveKellySizer(self.bankroll)
        self.binance = BinanceWebSocketFeed()
        self.news_scanner = NewsScanner()
        self.polymarket = PolymarketCLOBClient()
        self.sentiment = SentimentScorer()
        self.volatility_arb = VolatilityArbitrageEngine(self.binance, self.polymarket, self.bankroll)
        self.threshold_arb = ThresholdArbitrageEngine(self.binance, self.polymarket, self.bankroll)
        self.binance.on_volatility_spike = self.volatility_arb.on_volatility_spike_detected
    
    async def start(self):
        logger.info("\n" + "="*60)
        logger.info("🚀 POLYMARKET INTELLIGENT TRADING BOT")
        logger.info("="*60)
        settings.print_config()
        
        if not settings.validate():
            logger.error("Configuration validation failed")
            return
        
        self.running = True
        
        tasks = [
            asyncio.create_task(self.binance.listen(), name="binance_feed"),
            asyncio.create_task(self.news_monitoring_loop(), name="news_scanner"),
            asyncio.create_task(self.threshold_arb_loop(), name="threshold_arb"),
            asyncio.create_task(self.volatility_arb.monitor_panic_positions(), name="panic_monitor"),
            asyncio.create_task(self.status_report_loop(), name="status_report")
        ]
        
        await self.binance.connect()
        logger.info("✅ All systems operational\n")
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Tasks cancelled, shutting down...")
        except Exception as e:
            logger.error(f"Critical error: {e}", exc_info=True)
        finally:
            await self.shutdown()
    
    async def news_monitoring_loop(self):
        while self.running:
            try:
                can_trade, reason = self.circuit_breaker.check_should_trade()
                if not can_trade:
                    logger.warning(f"Trading halted: {reason}")
                    await asyncio.sleep(60)
                    continue
                
                news = await self.news_scanner.scan()
                if news:
                    sentiment_data = self.sentiment.analyze_news_batch(news[:10])
                    logger.info(f"📰 News: {len(news)} items | Sentiment: {sentiment_data['overall_sentiment']:+.2f}")
                    
                    if abs(sentiment_data['overall_sentiment']) > 0.5:
                        logger.info(f"🚨 Strong sentiment detected: {self.sentiment.score_to_label(sentiment_data['overall_sentiment'])}")
                
                await asyncio.sleep(settings.NEWS_SCAN_INTERVAL)
            except Exception as e:
                logger.error(f"News monitoring error: {e}")
                await asyncio.sleep(30)
    
    async def threshold_arb_loop(self):
        await asyncio.sleep(10)
        while self.running:
            try:
                can_trade, reason = self.circuit_breaker.check_should_trade()
                if not can_trade:
                    await asyncio.sleep(60)
                    continue
                
                opportunities = await self.threshold_arb.scan_opportunities()
                if opportunities:
                    await self.threshold_arb.execute_best_opportunity(opportunities)
                
                await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Threshold arb error: {e}")
                await asyncio.sleep(30)
    
    async def status_report_loop(self):
        while self.running:
            await asyncio.sleep(300)
            try:
                stats = self.bankroll.get_stats()
                logger.info("\n" + "="*60)
                logger.info("📊 PERFORMANCE REPORT")
                logger.info(f"Capital: ${stats['current_capital']:.2f} (ROI: {stats['roi']:+.1f}%)")
                logger.info(f"Trades: {stats['total_trades']} | Win Rate: {stats['win_rate']:.1%}")
                logger.info(f"P&L: ${stats['total_pnl']:+.2f} | Open: {stats['open_positions']}")
                logger.info(f"Drawdown: {stats['max_drawdown']:.1f}%")
                logger.info("="*60 + "\n")
                
                self.db.log_performance({
                    'bankroll': stats['current_capital'],
                    'open_positions': stats['open_positions'],
                    'total_trades': stats['total_trades'],
                    'winning_trades': int(stats['total_trades'] * stats['win_rate']),
                    'losing_trades': stats['total_trades'] - int(stats['total_trades'] * stats['win_rate']),
                    'win_rate': stats['win_rate'],
                    'total_pnl': stats['total_pnl'],
                    'roi': stats['roi'],
                    'max_drawdown': stats['max_drawdown']
                })
            except Exception as e:
                logger.error(f"Status report error: {e}")
    
    async def shutdown(self):
        logger.info("\n🛑 Shutting down bot...")
        self.running = False
        await self.binance.close()
        
        stats = self.bankroll.get_stats()
        logger.info("\n" + "="*60)
        logger.info("FINAL STATISTICS")
        logger.info(f"Starting Capital: ${settings.INITIAL_CAPITAL}")
        logger.info(f"Ending Capital: ${stats['current_capital']:.2f}")
        logger.info(f"Total Return: {stats['roi']:+.1f}%")
        logger.info(f"Total Trades: {stats['total_trades']}")
        logger.info(f"Win Rate: {stats['win_rate']:.1%}")
        logger.info("="*60)
        logger.info("✅ Bot stopped cleanly\n")

def signal_handler(signum, frame):
    logger.info("\n⚠️  Interrupt received, shutting down...")
    sys.exit(0)

async def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    bot = PolymarketBot()
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("✅ Bot stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)