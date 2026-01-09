import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging
from decimal import Decimal
from config.settings import settings
from utils.db import db

logger = logging.getLogger(__name__)

class VolatilityArbitrageEngine:
    def __init__(self, binance_feed, polymarket_client, position_manager, kelly_sizer):
        self.binance = binance_feed
        self.polymarket = polymarket_client
        self.position_manager = position_manager
        self.kelly_sizer = kelly_sizer
        
        self.active_panic_positions = {}
        self.volatility_events = []
        self.last_scan_time = {}
    
    async def on_volatility_spike(self, symbol: str, volatility_pct: float, current_price: float):
        if symbol in self.last_scan_time:
            if (datetime.utcnow() - self.last_scan_time[symbol]).total_seconds() < 30:
                return
        
        self.last_scan_time[symbol] = datetime.utcnow()
        
        logger.warning(f"VOLATILITY SPIKE DETECTED: {symbol} {volatility_pct:.2f}%")
        
        event_data = {
            "symbol": symbol,
            "volatility_pct": volatility_pct,
            "price": current_price,
            "direction": "UP" if volatility_pct > 0 else "DOWN",
            "opportunities_found": 0,
            "trades_executed": 0
        }
        
        opportunities = await self._scan_panic_prices(symbol, current_price)
        event_data["opportunities_found"] = len(opportunities)
        
        if opportunities:
            executed = await self._execute_panic_buys(opportunities, symbol)
            event_data["trades_executed"] = executed
        
        db.log_volatility_event(event_data)
        self.volatility_events.append(event_data)
    
    async def _scan_panic_prices(self, symbol: str, current_price: float) -> List[Dict]:
        markets = await self.polymarket.scan_crypto_markets_parallel([symbol])
        
        if not markets:
            return []
        
        price_data = await self.polymarket.get_market_prices_parallel(markets)
        
        opportunities = []
        for condition_id, data in price_data.items():
            yes_price = data["yes_price"]
            no_price = data["no_price"]
            liquidity = data["total_liquidity"]
            
            if liquidity < float(settings.MIN_MARKET_LIQUIDITY):
                continue
            
            if yes_price <= settings.EXTREME_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "condition_id": condition_id,
                    "market_title": data["market_title"],
                    "side": "YES",
                    "token_id": data["yes_token"],
                    "entry_price": yes_price,
                    "target_price": settings.PROFIT_TARGET_IDEAL,
                    "potential_multiple": settings.PROFIT_TARGET_IDEAL / yes_price if yes_price > 0 else 0,
                    "confidence": 0.90,
                    "liquidity": liquidity,
                    "discount_level": "EXTREME"
                })
            
            elif no_price <= settings.EXTREME_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "condition_id": condition_id,
                    "market_title": data["market_title"],
                    "side": "NO",
                    "token_id": data["no_token"],
                    "entry_price": no_price,
                    "target_price": settings.PROFIT_TARGET_IDEAL,
                    "potential_multiple": settings.PROFIT_TARGET_IDEAL / no_price if no_price > 0 else 0,
                    "confidence": 0.90,
                    "liquidity": liquidity,
                    "discount_level": "EXTREME"
                })
            
            elif yes_price <= settings.GOOD_DISCOUNT_THRESHOLD:
                opportunities.append({
                    "condition_id": condition_id,
                    "market_title": data["market_title"],
                    "side": "YES",
                    "token_id": data["yes_token"],
                    "entry_price": yes_price,
                    "target_price": settings.PROFIT_TARGET_MIN,
                    "potential_multiple": settings.PROFIT_TARGET_MIN / yes_price if yes_price > 0 else 0,
                    "confidence": 0.75,
                    "liquidity": liquidity,
                    "discount_level": "GOOD"
                })
        
        opportunities.sort(key=lambda x: x["potential_multiple"], reverse=True)
        return opportunities[:3]
    
    async def _execute_panic_buys(self, opportunities: List[Dict], symbol: str) -> int:
        executed = 0
        max_positions = 2
        
        current_positions = sum(1 for p in self.active_panic_positions.values() if p["symbol"] == symbol)
        if current_positions >= max_positions:
            return 0
        
        for opp in opportunities:
            if executed >= (max_positions - current_positions):
                break
            
            bankroll = Decimal("15.00")
            available = bankroll - self.position_manager.get_total_exposure()
            
            if available < settings.MIN_BET_SIZE:
                break
            
            payout_odds = opp["target_price"] / opp["entry_price"]
            bet_size = self.kelly_sizer.calculate_bet_size(
                bankroll=available,
                win_probability=opp["confidence"],
                payout_odds=payout_odds,
                edge=opp["potential_multiple"] / 10,
                volatility_regime="extreme"
            )
            
            if bet_size < settings.MIN_BET_SIZE:
                continue
            
            bet_size = min(bet_size, available * Decimal("0.15"))
            
            logger.info(f"PANIC BUY: {opp['market_title'][:50]}")
            logger.info(f"  {opp['side']} @ ${opp['entry_price']:.3f} | Bet: ${bet_size:.2f} | {opp['potential_multiple']:.1f}x")
            
            shares = float(bet_size) / opp["entry_price"]
            result = await self.polymarket.place_order(
                token_id=opp["token_id"],
                side="BUY",
                amount=shares,
                price=opp["entry_price"] * 1.05
            )
            
            if result:
                position_id = result.get("order_id", f"panic_{int(datetime.utcnow().timestamp())}")
                
                position = self.position_manager.open_position(
                    position_id=position_id,
                    market_id=opp["condition_id"],
                    market_title=opp["market_title"],
                    strategy="volatility_arbitrage",
                    side=opp["side"],
                    entry_price=opp["entry_price"],
                    amount=bet_size,
                    confidence=opp["confidence"],
                    edge=opp["potential_multiple"] / 10
                )
                
                if position:
                    self.active_panic_positions[position_id] = {
                        "symbol": symbol,
                        "target_price": opp["target_price"],
                        "token_id": opp["token_id"],
                        "shares": shares
                    }
                    
                    db.log_trade({
                        "strategy": "volatility_arbitrage",
                        "market_id": opp["condition_id"],
                        "market_title": opp["market_title"],
                        "side": opp["side"],
                        "entry_price": opp["entry_price"],
                        "amount": float(bet_size),
                        "confidence": opp["confidence"],
                        "edge": opp["potential_multiple"] / 10,
                        "status": "OPEN"
                    })
                    
                    executed += 1
        
        return executed
    
    async def monitor_positions(self):
        while True:
            await asyncio.sleep(30)
            
            if not self.active_panic_positions:
                continue
            
            for position_id in list(self.active_panic_positions.keys()):
                position = self.position_manager.positions.get(position_id)
                if not position:
                    del self.active_panic_positions[position_id]
                    continue
                
                panic_data = self.active_panic_positions[position_id]
                
                orderbook = await self.polymarket.get_market_orderbook(panic_data["token_id"])
                current_price = self.polymarket._get_best_price(orderbook, "bid")
                
                should_exit = False
                exit_reason = ""
                
                if current_price >= panic_data["target_price"]:
                    should_exit = True
                    exit_reason = "TARGET_HIT"
                elif current_price < position.entry_price * 0.5:
                    should_exit = True
                    exit_reason = "STOP_LOSS"
                elif position.get_hold_time_minutes() > 360:
                    if current_price > position.entry_price:
                        should_exit = True
                        exit_reason = "TIME_STOP_PROFITABLE"
                
                if should_exit:
                    result = await self.polymarket.place_order(
                        token_id=panic_data["token_id"],
                        side="SELL",
                        amount=panic_data["shares"],
                        price=current_price * 0.95
                    )
                    
                    if result:
                        close_result = self.position_manager.close_position(
                            position_id=position_id,
                            exit_price=current_price,
                            exit_reason=exit_reason
                        )
                        
                        if close_result:
                            db.update_trade(position_id, {
                                "exit_price": current_price,
                                "profit": close_result["realized_pnl"],
                                "roi": close_result["roi"],
                                "status": "CLOSED",
                                "exit_timestamp": datetime.utcnow(),
                                "exit_reason": exit_reason
                            })
                            
                            self.kelly_sizer.record_trade_result(
                                win=close_result["realized_pnl"] > 0,
                                profit=close_result["realized_pnl"]
                            )
                        
                        del self.active_panic_positions[position_id]