"""
Volatility Arbitrage Engine
$131K bot strategy: Buy panic-sold positions, sell on recovery
"""
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class VolatilityArbitrageEngine:
    """Exploit volatility-induced mispricings"""
    
    def __init__(self, binance_feed, polymarket_client, bankroll_tracker):
        self.binance = binance_feed
        self.polymarket = polymarket_client
        self.bankroll = bankroll_tracker
        
        # Thresholds
        self.PANIC_PRICE_THRESHOLD = settings.PANIC_BUY_MAX_PRICE
        self.PROFIT_TARGET = settings.PANIC_SELL_TARGET
        self.MAX_POSITIONS = settings.MAX_PANIC_POSITIONS
        
        # Tracking
        self.active_positions = []
        self.volatility_events = []
    
    async def on_volatility_spike(self, symbol: str, volatility_pct: float, price: float):
        """Triggered when >3% move detected"""
        logger.warning(f"🚨 SPIKE: {symbol} moved {volatility_pct:.2f}%")
        
        event = {
            "symbol": symbol,
            "volatility": volatility_pct,
            "price": price,
            "timestamp": datetime.utcnow()
        }
        self.volatility_events.append(event)
        
        # Scan for panic-priced markets
        opportunities = await self._scan_panic_markets(symbol, price)
        
        if opportunities:
            logger.info(f"💎 {len(opportunities)} panic opportunities")
            await self._execute_panic_buys(opportunities)
    
    async def _scan_panic_markets(self, symbol: str, current_price: float) -> List[Dict]:
        """Find markets with crashed odds"""
        markets = await self.polymarket.scan_markets_parallel([symbol])
        opportunities = []
        
        for market in markets:
            yes_price = market.get("yes_price", 0.5)
            no_price = market.get("no_price", 0.5)
            liquidity = market.get("liquidity", 0)
            
            # Find panic-sold side
            if yes_price <= self.PANIC_PRICE_THRESHOLD and liquidity >= 100:
                potential_gain = (self.PROFIT_TARGET / yes_price) if yes_price > 0 else 0
                
                opportunities.append({
                    "market_id": market["id"],
                    "question": market["question"],
                    "side": "YES",
                    "entry_price": yes_price,
                    "target_price": self.PROFIT_TARGET,
                    "potential_multiple": potential_gain,
                    "liquidity": liquidity,
                    "confidence": 0.85
                })
            
            elif no_price <= self.PANIC_PRICE_THRESHOLD and liquidity >= 100:
                potential_gain = (self.PROFIT_TARGET / no_price) if no_price > 0 else 0
                
                opportunities.append({
                    "market_id": market["id"],
                    "question": market["question"],
                    "side": "NO",
                    "entry_price": no_price,
                    "target_price": self.PROFIT_TARGET,
                    "potential_multiple": potential_gain,
                    "liquidity": liquidity,
                    "confidence": 0.85
                })
        
        # Sort by potential gain
        opportunities.sort(key=lambda x: x["potential_multiple"], reverse=True)
        return opportunities[:3]
    
    async def _execute_panic_buys(self, opportunities: List[Dict]):
        """Execute panic buy orders"""
        if len(self.active_positions) >= self.MAX_POSITIONS:
            return
        
        available = self.bankroll.get_available_capital()
        max_bet = available * 0.15  # 15% per panic position
        
        for opp in opportunities[:self.MAX_POSITIONS - len(self.active_positions)]:
            # Check liquidity depth
            depth = self.polymarket.check_liquidity_depth({"liquidity": opp["liquidity"]})
            if not depth["sufficient"]:
                continue
            
            # Size position
            bet_size = min(max_bet, available * 0.15)
            bet_size = max(bet_size, 0.50)
            
            logger.info(f"💰 PANIC BUY: {opp['question'][:50]}...")
            logger.info(f"   {opp['side']} @ ${opp['entry_price']:.3f} | Target: {opp['potential_multiple']:.1f}x")
            
            # Execute
            success = await self.polymarket.place_bet(
                market_id=opp["market_id"],
                side=opp["side"],
                amount=bet_size,
                max_price=opp["entry_price"] * 1.1
            )
            
            if success:
                position = {
                    **opp,
                    "bet_size": bet_size,
                    "entry_time": datetime.utcnow(),
                    "status": "OPEN"
                }
                self.active_positions.append(position)
    
    async def monitor_positions(self):
        """Background task: Monitor and exit positions"""
        while True:
            for position in self.active_positions[:]:
                current_price = await self.polymarket.get_position_value(
                    position["market_id"],
                    position["side"]
                )
                
                if not current_price:
                    await asyncio.sleep(10)
                    continue
                
                # Exit conditions
                should_exit = False
                reason = ""
                
                # Profit target hit
                if current_price >= position["target_price"]:
                    should_exit = True
                    reason = "PROFIT TARGET"
                
                # Stop loss
                elif current_price < position["entry_price"] * 0.5:
                    should_exit = True
                    reason = "STOP LOSS"
                
                # Time stop (6 hours)
                elif (datetime.utcnow() - position["entry_time"]) > timedelta(hours=6):
                    if current_price > position["entry_price"]:
                        should_exit = True
                        reason = "TIME STOP (PROFITABLE)"
                
                if should_exit:
                    # Calculate P&L
                    shares = position["bet_size"] / position["entry_price"]
                    exit_value = shares * current_price
                    profit = exit_value - position["bet_size"]
                    roi = (profit / position["bet_size"]) * 100
                    
                    logger.info(f"🎯 EXIT: {reason}")
                    logger.info(f"   {position['question'][:40]}...")
                    logger.info(f"   ${position['entry_price']:.3f} → ${current_price:.3f}")
                    logger.info(f"   P&L: ${profit:+.2f} ({roi:+.1f}%)")
                    
                    self.active_positions.remove(position)
            
            await asyncio.sleep(30)
    
    def get_stats(self) -> Dict:
        return {
            "active_positions": len(self.active_positions),
            "total_events": len(self.volatility_events)
        }