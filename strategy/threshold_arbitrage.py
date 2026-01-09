import asyncio
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)

class ThresholdArbitrageEngine:
    def __init__(self, binance_feed, polymarket_client, bankroll_tracker):
        self.binance = binance_feed
        self.polymarket = polymarket_client
        self.bankroll = bankroll_tracker
        self.MIN_EDGE = 0.15
    
    async def scan_opportunities(self) -> List[Dict]:
        symbols = ["BTC", "ETH", "SOL"]
        binance_prices = {
            sym: {"price": self.binance.get_current_price(sym) or 0}
            for sym in symbols
        }
        
        opportunities = await self.polymarket.scan_markets_parallel(symbols, binance_prices)
        filtered = [opp for opp in opportunities if opp['edge'] >= self.MIN_EDGE]
        
        if filtered:
            logger.info(f"⚡ Found {len(filtered)} threshold arbitrage opportunities")
            for opp in filtered[:3]:
                logger.info(f"   {opp['symbol']}: {opp['question'][:50]}... | Edge: {opp['edge']:.1%}")
        
        return filtered
    
    async def execute_best_opportunity(self, opportunities: List[Dict]) -> bool:
        if not opportunities:
            return False
        
        best = opportunities[0]
        available_capital = self.bankroll.get_available_capital()
        bet_size = min(available_capital * 0.20, available_capital * best['edge'])
        bet_size = max(bet_size, 0.50)
        
        logger.info(f"🎯 EXECUTING: {best['question'][:60]}...")
        logger.info(f"   Outcome: {best['true_outcome']} | Edge: {best['edge']:.1%} | Bet: ${bet_size:.2f}")
        
        success = await self.polymarket.place_bet(
            market_id=best['market_id'],
            side=best['true_outcome'],
            amount=bet_size,
            max_price=best['market_price'] * 1.05
        )
        
        return success