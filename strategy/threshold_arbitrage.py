import asyncio
from typing import Dict, List, Optional
from datetime import datetime
import logging
from decimal import Decimal
from config.settings import settings
from config.markets import CRYPTO_SYMBOLS
from utils.db import db

logger = logging.getLogger(__name__)

class ThresholdArbitrageEngine:
    def __init__(self, binance_feed, polymarket_client, position_manager, kelly_sizer):
        self.binance = binance_feed
        self.polymarket = polymarket_client
        self.position_manager = position_manager
        self.kelly_sizer = kelly_sizer
        
        self.active_arb_positions = {}
        self.last_check = {}
    
    async def scan_opportunities(self):
        markets = await self.polymarket.scan_crypto_markets_parallel()
        
        if not markets:
            return []
        
        price_data = await self.polymarket.get_market_prices_parallel(markets)
        
        opportunities = []
        
        for condition_id, data in price_data.items():
            opp = await self._check_threshold_arbitrage(data)
            if opp:
                opportunities.append(opp)
        
        return opportunities
    
    async def _check_threshold_arbitrage(self, market_data: Dict) -> Optional[Dict]:
        title = market_data["market_title"].lower()
        
        for symbol, config in CRYPTO_SYMBOLS.items():
            if any(tag in title for tag in config["polymarket_tags"]):
                current_price = self.binance.get_current_price(symbol)
                if not current_price:
                    continue
                
                threshold = self._extract_threshold(title)
                if not threshold:
                    continue
                
                is_above_market = "above" in title or "over" in title or "exceed" in title
                is_below_market = "below" in title or "under" in title
                
                if not (is_above_market or is_below_market):
                    continue
                
                edge = None
                side = None
                confidence = 0.0
                
                if is_above_market:
                    if current_price > threshold * 1.02:
                        side = "YES"
                        confidence = 0.95
                        edge = (current_price - threshold) / threshold
                    elif current_price < threshold * 0.98:
                        side = "NO"
                        confidence = 0.95
                        edge = (threshold - current_price) / threshold
                
                elif is_below_market:
                    if current_price < threshold * 0.98:
                        side = "YES"
                        confidence = 0.95
                        edge = (threshold - current_price) / threshold
                    elif current_price > threshold * 1.02:
                        side = "NO"
                        confidence = 0.95
                        edge = (current_price - threshold) / threshold
                
                if side and edge and edge > 0.02:
                    market_price = market_data["yes_price"] if side == "YES" else market_data["no_price"]
                    
                    if market_price > 0.90:
                        continue
                    
                    implied_edge = (1 / market_price - 1) * confidence - (1 - confidence)
                    
                    if implied_edge > settings.MIN_EDGE_THRESHOLD:
                        return {
                            "condition_id": market_data["condition_id"],
                            "market_title": market_data["market_title"],
                            "symbol": symbol,
                            "side": side,
                            "token_id": market_data[f"{side.lower()}_token"],
                            "entry_price": market_price,
                            "current_crypto_price": current_price,
                            "threshold": threshold,
                            "edge": implied_edge,
                            "confidence": confidence,
                            "liquidity": market_data["total_liquidity"]
                        }
        
        return None
    
    def _extract_threshold(self, title: str) -> Optional[float]:
        import re
        
        patterns = [
            r'\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)',
            r'(\d{1,3}(?:,\d{3})*(?:\.\d+)?)(?:\s)?k',
            r'(\d+(?:\.\d+)?)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, title.replace(',', ''))
            if match:
                value = float(match.group(1).replace(',', ''))
                if 'k' in title.lower():
                    value *= 1000
                return value
        
        return None
    
    async def execute_opportunities(self, opportunities: List[Dict]) -> int:
        executed = 0
        
        for opp in opportunities[:3]:
            if self.position_manager.get_position_count() >= settings.MAX_OPEN_POSITIONS:
                break
            
            bankroll = Decimal("15.00")
            available = bankroll - self.position_manager.get_total_exposure()
            
            if available < settings.MIN_BET_SIZE:
                break
            
            payout_odds = 1 / opp["entry_price"]
            bet_size = self.kelly_sizer.calculate_bet_size(
                bankroll=available,
                win_probability=opp["confidence"],
                payout_odds=payout_odds,
                edge=opp["edge"],
                volatility_regime="normal"
            )
            
            if bet_size < settings.MIN_BET_SIZE:
                continue
            
            logger.info(f"THRESHOLD ARB: {opp['market_title'][:50]}")
            logger.info(f"  {opp['side']} @ ${opp['entry_price']:.3f} | Edge: {opp['edge']:.2%} | Bet: ${bet_size:.2f}")
            
            shares = float(bet_size) / opp["entry_price"]
            result = await self.polymarket.place_order(
                token_id=opp["token_id"],
                side="BUY",
                amount=shares,
                price=opp["entry_price"] * 1.03
            )
            
            if result:
                position_id = result.get("order_id", f"threshold_{int(datetime.utcnow().timestamp())}")
                
                position = self.position_manager.open_position(
                    position_id=position_id,
                    market_id=opp["condition_id"],
                    market_title=opp["market_title"],
                    strategy="threshold_arbitrage",
                    side=opp["side"],
                    entry_price=opp["entry_price"],
                    amount=bet_size,
                    confidence=opp["confidence"],
                    edge=opp["edge"]
                )
                
                if position:
                    self.active_arb_positions[position_id] = {
                        "symbol": opp["symbol"],
                        "threshold": opp["threshold"],
                        "token_id": opp["token_id"],
                        "shares": shares
                    }
                    
                    db.log_trade({
                        "strategy": "threshold_arbitrage",
                        "market_id": opp["condition_id"],
                        "market_title": opp["market_title"],
                        "side": opp["side"],
                        "entry_price": opp["entry_price"],
                        "amount": float(bet_size),
                        "confidence": opp["confidence"],
                        "edge": opp["edge"],
                        "status": "OPEN"
                    })
                    
                    executed += 1
        
        return executed