import asyncio
import signal
import sys
from decimal import Decimal
import logging
from datetime import datetime

from config.settings import settings
from utils.logger import setup_logger, performance_logger
from utils.db import db

from data_feeds.binance_websocket import BinanceWebSocketFeed
from data_feeds.polymarket_client import PolymarketClient

from risk.kelly_sizer import AdaptiveKellySizer
from risk.position_manager import PositionManager
from risk.circuit_breaker import CircuitBreaker

from strategy.volatility_arbitrage import VolatilityArbitrageEngine
from strategy.threshold_arbitrage import ThresholdArbitrageEngine

logger = setup_logger()

class PolymarketTradingBot:
    def __init__(self):
        self.running = False
        
        self.binance = BinanceWebSocketFeed()
        self.polymarket = PolymarketClient()
        
        self.kelly_sizer = AdaptiveKellySizer()
        self.position_manager = PositionManager()
        self.circuit_breaker = CircuitBreaker(settings.INITIAL_CAPITAL)
        
        self.volatility_engine = VolatilityArbitrageEngine(
            self.binance,
            self.polymarket,
            self.position_manager,
            self.kelly_sizer
        )
        
        self.threshold_engine = ThresholdArbitrageEngine(
            self.binance,
            self.polymarket,
            self.position_manager,
            self.kelly_sizer
        )
        
        self.current_capital = settings.INITIAL_CAPITAL
        self.start_time = datetime.utcnow()
        
        self.binance.on_volatility_spike = self.volatility_engine.on_volatility_spike
    
    async def start(self):
        if not settings.validate():
            logger.error("Configuration validation failed")
            return False
        
        settings.log_config()
        
        logger.info("Starting Polymarket Trading Bot V2")
        logger.info(f"Mode: {'PAPER TRADING' if settings.PAPER_TRADING else 'LIVE TRADING'}")
        
        self.running = True
        
        tasks = [
            asyncio.create_task(self.binance.listen()),
            asyncio.create_task(self.volatility_engine.monitor_positions()),
            asyncio.create_task(self.threshold_scan_loop()),
            asyncio.create_task(self.performance_monitor_loop()),
        ]
        
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot tasks cancelled")
        finally:
            await self.shutdown()
    
    async def threshold_scan_loop(self):
        await asyncio.sleep(10)
        
        while self.running:
            try:
                if not self.circuit_breaker.is_trading_allowed():
                    logger.warning("Trading paused by circuit breaker")
                    await asyncio.sleep(60)
                    continue
                
                opportunities = await self.threshold_engine.scan_opportunities()
                
                if opportunities:
                    logger.info(f"Found {len(opportunities)} threshold arbitrage opportunities")
                    executed = await self.threshold_engine.execute_opportunities(opportunities)
                    if executed > 0:
                        logger.info(f"Executed {executed} threshold arbitrage trades")
                
                await asyncio.sleep(settings.PRICE_CHECK_INTERVAL)
                
            except Exception as e:
                logger.error(f"Threshold scan error: {e}", exc_info=True)
                await asyncio.sleep(5)
    
    async def performance_monitor_loop(self):
        while self.running:
            await asyncio.sleep(300)
            
            try:
                stats = self.position_manager.get_statistics()
                breaker_status = self.circuit_breaker.get_status()
                
                logger.info("=" * 60)
                logger.info("PERFORMANCE UPDATE")
                logger.info(f"Capital: ${self.current_capital:.2f}")
                logger.info(f"Open Positions: {stats['open_positions']}")
                logger.info(f"Exposure: ${stats['total_exposure']:.2f}")
                logger.info(f"Unrealized P&L: ${stats['unrealized_pnl']:+.2f}")
                logger.info(f"Win Rate: {stats['win_rate']:.1%}")
                logger.info(f"Drawdown: {breaker_status['current_drawdown']:.2f}%")
                logger.info(f"Trades Today: {breaker_status['trades_today']}")
                logger.info("=" * 60)
                
                db.log_performance({
                    "capital": float(self.current_capital),
                    "open_positions": stats['open_positions'],
                    "daily_pnl": 0.0,
                    "total_pnl": float(self.current_capital - settings.INITIAL_CAPITAL),
                    "win_rate": stats['win_rate'],
                    "max_drawdown": breaker_status['current_drawdown']
                })
                
            except Exception as e:
                logger.error(f"Performance monitor error: {e}")
    
    async def shutdown(self):
        logger.info("Shutting down bot...")
        self.running = False
        
        await self.binance.close()
        await self.polymarket.close()
        
        logger.info("Bot shutdown complete")
    
    def handle_signal(self, sig, frame):
        logger.info(f"Received signal {sig}")
        self.running = False
        
        for task in asyncio.all_tasks():
            task.cancel()

async def main():
    bot = PolymarketTradingBot()
    
    signal.signal(signal.SIGINT, bot.handle_signal)
    signal.signal(signal.SIGTERM, bot.handle_signal)
    
    await bot.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)