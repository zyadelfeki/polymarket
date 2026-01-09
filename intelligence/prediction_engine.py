"""
Prediction Engine
Ensemble system combining all signals for trading decisions
"""
from typing import Dict, List, Optional
import logging
from intelligence.edge_calculator import EdgeCalculator

logger = logging.getLogger(__name__)

class PredictionEngine:
    """Ensemble prediction system"""
    
    def __init__(self, news_scanner, sentiment_scorer, binance_feed):
        self.news_scanner = news_scanner
        self.sentiment = sentiment_scorer
        self.binance = binance_feed
        self.edge_calc = EdgeCalculator()
    
    async def analyze_market(self, market: Dict, symbol: str) -> Optional[Dict]:
        """
        Full analysis of a market
        Returns trading signal with confidence
        """
        # Get current price
        current_price = self.binance.get_price(symbol)
        if not current_price:
            return None
        
        # Check threshold arbitrage first (highest confidence)
        threshold_arb = self.edge_calc.check_price_threshold_arb(
            market, current_price, 0
        )
        if threshold_arb and threshold_arb["confidence"] > 0.95:
            return threshold_arb
        
        # Get news sentiment
        recent_news = self.news_scanner.get_latest(10)
        sentiment_result = self.sentiment.analyze_batch(recent_news)
        news_sentiment = sentiment_result.get("overall_sentiment", 0.0)
        
        # Build signal dict
        signals = {
            "news_sentiment": news_sentiment,
            "price_signal": 0.5,  # Neutral unless threshold crossed
            "confidence": sentiment_result.get("confidence", 0.5)
        }
        
        # Calculate edge
        edge_result = self.edge_calc.calculate_edge(market, signals)
        
        if edge_result["signal"] != "SKIP":
            logger.info(f"✅ SIGNAL: {edge_result['signal']} on {market['question'][:40]}...")
            logger.info(f"   Edge: {edge_result['edge']:.1%} | Confidence: {edge_result['confidence']:.1%}")
        
        return edge_result
    
    async def scan_all_opportunities(self, markets: List[Dict]) -> List[Dict]:
        """Scan all markets for opportunities"""
        opportunities = []
        
        for market in markets:
            # Determine symbol from question
            question = market.get("question", "").lower()
            symbol = None
            
            if "btc" in question or "bitcoin" in question:
                symbol = "BTC"
            elif "eth" in question or "ethereum" in question:
                symbol = "ETH"
            elif "sol" in question or "solana" in question:
                symbol = "SOL"
            
            if not symbol:
                continue
            
            signal = await self.analyze_market(market, symbol)
            
            if signal and signal["signal"] != "SKIP":
                opportunities.append({
                    "market": market,
                    "signal": signal,
                    "symbol": symbol
                })
        
        # Sort by edge * confidence
        opportunities.sort(
            key=lambda x: x["signal"]["edge"] * x["signal"]["confidence"],
            reverse=True
        )
        
        return opportunities