import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class VolatilityArbitrageEngine:
    def __init__(self, binance_feed, polymarket_client, bankroll_tracker):
        self.binance = binance_feed
        self.polymarket = polymarket_client
        self.bankroll = bankroll_tracker
        self.EXTREME_DISCOUNT_THRESHOLD = 0.05
        self.GOOD_DISCOUNT_THRESHOLD = 0.10
        self.PROFIT_TARGET_MIN = 0.30
        self.PROFIT_TARGET_IDEAL = 0.60
        self.MAX_PANIC_BET_PCT = 0.15
        self.MAX_SIMULTANEOUS_PANIC_POSITIONS = 2
        self.active_panic_positions = []
        self.volatility_events = []
    
    async def on_volatility_spike_detected(self, symbol: str, volatility_pct: float, current_price: float):
        logger.warning(f"🚨 VOLATILITY SPIKE: {symbol} moved {volatility_pct:.2f}%")
        event = {
            "symbol": symbol,
            "volatility": volatility_pct,
            "price": current_price,
            "timestamp": datetime.utcnow(),
            "direction": "UP" if volatility_pct > 0 else "DOWN"
        }
        self.volatility_events.append(event)
        opportunities = await self._scan_for_panic_prices(symbol, event)
        if opportunities:
            logger.info(f"💎 Found {len(opportunities)} panic-priced opportunities!")
            await self._execute_panic_buys(opportunities)
        else:
            logger.info("No panic-priced markets found")
    
    async def _scan_for_panic_prices(self, symbol: str, event: Dict) -> List[Dict]:
        opportunities = []
        markets = await self.polymarket.get_active_crypto_markets()
        
        for market in markets:
            question = market.get('question', '').lower()
            if symbol.lower() not in question:
                continue
            
            tokens = market.get('tokens', [])
            if len(tokens) < 2:
                continue
            
            yes_price = float(tokens[0].get('price', 1.0))
            no_price = float(tokens[1].get('price', 1.0))
            
            if yes_price <= self.EXTREME_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "market_id": market.get('condition_id'),
                    "market_title": market.get('question'),
                    "side": "YES",
                    "current_price": yes_price,
                    "expected_recovery_price": self.PROFIT_TARGET_IDEAL,
                    "potential_multiple": self.PROFIT_TARGET_IDEAL / yes_price if yes_price > 0 else 0,
                    "confidence": 0.90,
                    "liquidity": market.get('liquidity', 0)
                })
            elif no_price <= self.EXTREME_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "market_id": market.get('condition_id'),
                    "market_title": market.get('question'),
                    "side": "NO",
                    "current_price": no_price,
                    "expected_recovery_price": self.PROFIT_TARGET_IDEAL,
                    "potential_multiple": self.PROFIT_TARGET_IDEAL / no_price if no_price > 0 else 0,
                    "confidence": 0.90,
                    "liquidity": market.get('liquidity', 0)
                })
            elif yes_price <= self.GOOD_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "market_id": market.get('condition_id'),
                    "market_title": market.get('question'),
                    "side": "YES",
                    "current_price": yes_price,
                    "expected_recovery_price": self.PROFIT_TARGET_MIN,
                    "potential_multiple": self.PROFIT_TARGET_MIN / yes_price if yes_price > 0 else 0,
                    "confidence": 0.75,
                    "liquidity": market.get('liquidity', 0)
                })
        
        opportunities.sort(key=lambda x: x['potential_multiple'], reverse=True)
        return opportunities[:5]
    
    async def _execute_panic_buys(self, opportunities: List[Dict]):
        if len(self.active_panic_positions) >= self.MAX_SIMULTANEOUS_PANIC_POSITIONS:
            logger.warning("⚠️  Max panic positions reached")
            return
        
        available_capital = self.bankroll.get_available_capital()
        max_bet_size = available_capital * self.MAX_PANIC_BET_PCT
        executed = 0
        
        for opp in opportunities:
            if executed >= (self.MAX_SIMULTANEOUS_PANIC_POSITIONS - len(self.active_panic_positions)):
                break
            if opp["liquidity"] < 100:
                continue
            
            bet_multiplier = min(opp["potential_multiple"] / 10, 1.5)
            bet_size = min(max_bet_size * bet_multiplier, max_bet_size)
            bet_size = max(bet_size, 0.50)
            
            logger.info(f"💰 PANIC BUY: {opp['market_title']}")
            logger.info(f"   Side: {opp['side']} at ${opp['current_price']:.3f}")
            logger.info(f"   Bet: ${bet_size:.2f} | Potential: {opp['potential_multiple']:.1f}x")
            
            success = await self.polymarket.place_bet(
                market_id=opp["market_id"],
                side=opp["side"],
                amount=bet_size,
                max_price=opp["current_price"] * 1.1
            )
            
            if success:
                position = {**opp, "bet_size": bet_size, "entry_time": datetime.utcnow(), "status": "OPEN"}
                self.active_panic_positions.append(position)
                executed += 1
                logger.info(f"✅ Executed panic buy #{executed}")
    
    async def monitor_panic_positions(self):
        while True:
            if not self.active_panic_positions:
                await asyncio.sleep(10)
                continue
            
            for position in self.active_panic_positions[:]:
                current_price = await self.polymarket.get_market_price(position["market_id"], position["side"])
                if current_price is None:
                    continue
                
                should_exit = False
                exit_reason = ""
                
                if current_price >= position["expected_recovery_price"]:
                    should_exit = True
                    exit_reason = f"PROFIT TARGET (${current_price:.3f})"
                elif current_price < position["current_price"] * 0.5:
                    should_exit = True
                    exit_reason = f"STOP LOSS (${current_price:.3f})"
                elif (datetime.utcnow() - position["entry_time"]) > timedelta(hours=6):
                    if current_price > position["current_price"]:
                        should_exit = True
                        exit_reason = f"TIME STOP - PROFITABLE (${current_price:.3f})"
                
                if should_exit:
                    success = await self.polymarket.sell_position(
                        market_id=position["market_id"],
                        side=position["side"],
                        amount=position["bet_size"] / position["current_price"],
                        min_price=current_price * 0.95
                    )
                    
                    if success:
                        profit = (current_price - position["current_price"]) * (position["bet_size"] / position["current_price"])
                        roi = (profit / position["bet_size"]) * 100
                        logger.info(f"🎯 PANIC POSITION CLOSED: {exit_reason}")
                        logger.info(f"   P&L: ${profit:+.2f} ({roi:+.1f}%)")
                        self.active_panic_positions.remove(position)
            
            await asyncio.sleep(30)
    
    def get_stats(self) -> Dict:
        return {
            "active_positions": len(self.active_panic_positions),
            "total_volatility_events": len(self.volatility_events)
        }