#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_feeds.binance_websocket import BinanceWebSocketFeed
from data_feeds.news_scanner import NewsScanner
from intelligence.sentiment_scorer import SentimentScorer
from utils.logger import setup_logger

logger = setup_logger("test", "INFO")

async def test_binance():
    logger.info("\n" + "="*60)
    logger.info("⚡ TESTING BINANCE WEBSOCKET")
    logger.info("="*60)
    
    feed = BinanceWebSocketFeed()
    
    async def on_price(symbol, price, data):
        volatility = feed.get_volatility(symbol)
        logger.info(f"{symbol}: ${price:,.2f} | Vol: {volatility:.2f}%")
    
    async def on_vol(symbol, vol, price):
        logger.warning(f"🚨 {symbol} VOLATILITY SPIKE: {vol:.2f}% at ${price:,.2f}")
    
    feed.on_price_update = on_price
    feed.on_volatility_spike = on_vol
    
    await feed.connect()
    logger.info("✅ Listening for 30 seconds...\n")
    
    listen_task = asyncio.create_task(feed.listen())
    await asyncio.sleep(30)
    await feed.close()
    listen_task.cancel()
    
    logger.info("\n✅ Binance test complete")

async def test_news():
    logger.info("\n" + "="*60)
    logger.info("📰 TESTING NEWS SCANNER")
    logger.info("="*60)
    
    scanner = NewsScanner()
    news = await scanner.scan()
    
    logger.info(f"\nFound {len(news)} news items\n")
    for i, item in enumerate(news[:5], 1):
        logger.info(f"{i}. [{item.source}] {item.title}")
        logger.info(f"   Relevance: {item.relevance_score:.2f}\n")
    
    logger.info("✅ News test complete")

async def test_sentiment():
    logger.info("\n" + "="*60)
    logger.info("🧠 TESTING SENTIMENT ANALYZER")
    logger.info("="*60)
    
    scorer = SentimentScorer()
    
    tests = [
        "Bitcoin surges to new all-time high as institutions buy",
        "Crypto market crashes amid regulatory fears",
        "Ethereum consolidates near support level"
    ]
    
    for text in tests:
        result = scorer.analyze_text(text)
        label = scorer.score_to_label(result['ensemble_score'])
        logger.info(f"\nText: {text}")
        logger.info(f"Score: {result['ensemble_score']:+.2f} | {label}")
    
    logger.info("\n✅ Sentiment test complete")

async def main():
    logger.info("\n🚀 TESTING ALL DATA FEEDS\n")
    
    await test_binance()
    await asyncio.sleep(2)
    await test_news()
    await asyncio.sleep(2)
    await test_sentiment()
    
    logger.info("\n" + "="*60)
    logger.info("✅ ALL TESTS PASSED")
    logger.info("="*60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())