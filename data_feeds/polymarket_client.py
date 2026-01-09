"""
Polymarket CLOB API Client
Production-ready integration with parallel market scanning
"""
import asyncio
import aiohttp
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from config.settings import settings
from config.markets import market_config

logger = logging.getLogger(__name__)

class PolymarketClient:
    """High-performance Polymarket trading client"""
    
    def __init__(self, private_key: Optional[str] = None):
        self.private_key = private_key or settings.POLYMARKET_PRIVATE_KEY
        self.paper_trading = settings.PAPER_TRADING
        
        # Initialize CLOB client if not paper trading
        if not self.paper_trading and self.private_key:
            try:
                self.client = ClobClient(
                    host="https://clob.polymarket.com",
                    key=self.private_key,
                    chain_id=137  # Polygon mainnet
                )
                logger.info("✅ Polymarket CLOB client initialized")
            except Exception as e:
                logger.error(f"❌ CLOB init failed: {e}")
                self.client = None
        else:
            self.client = None
            logger.info("📝 Paper trading mode - no real execution")
        
        # Market cache
        self.market_cache = {}
        self.last_market_refresh = None
    
    async def scan_markets_parallel(self, symbols: List[str] = ["BTC", "ETH", "SOL"]) -> List[Dict]:
        """
        Scan multiple markets in parallel
        Target: 50+ markets in <3 seconds
        """
        tasks = []
        
        for symbol in symbols:
            task = self._fetch_markets_for_symbol(symbol)
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_markets = []
        for result in results:
            if isinstance(result, list):
                all_markets.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Market scan error: {result}")
        
        # Filter and rank
        filtered = self._filter_markets(all_markets)
        return filtered
    
    async def _fetch_markets_for_symbol(self, symbol: str) -> List[Dict]:
        """Fetch all markets for a cryptocurrency"""
        try:
            # Public API endpoint for market data
            async with aiohttp.ClientSession() as session:
                url = "https://clob.polymarket.com/markets"
                params = {"active": "true", "closed": "false"}
                
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Filter for crypto markets
                        crypto_markets = []
                        for market in data:
                            question = market.get("question", "").lower()
                            if symbol.lower() in question:
                                crypto_markets.append(self._parse_market(market))
                        
                        logger.debug(f"✅ {symbol}: {len(crypto_markets)} markets")
                        return crypto_markets
                    else:
                        return []
        except Exception as e:
            logger.error(f"Fetch error {symbol}: {e}")
            return []
    
    def _parse_market(self, raw_market: Dict) -> Dict:
        """Parse raw market data into standardized format"""
        try:
            # Extract key fields
            market_id = raw_market.get("condition_id", "")
            question = raw_market.get("question", "")
            end_date = raw_market.get("end_date_iso", "")
            
            # Calculate time to resolution
            hours_to_resolve = None
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_to_resolve = (end_dt - datetime.utcnow()).total_seconds() / 3600
                except:
                    pass
            
            # Get current odds
            tokens = raw_market.get("tokens", [])
            yes_price = 0.5
            no_price = 0.5
            
            if len(tokens) >= 2:
                # Typically tokens[0] is YES, tokens[1] is NO
                for token in tokens:
                    outcome = token.get("outcome", "").lower()
                    price = float(token.get("price", 0.5))
                    
                    if "yes" in outcome:
                        yes_price = price
                    elif "no" in outcome:
                        no_price = price
            
            # Liquidity
            liquidity = float(raw_market.get("liquidity", 0))
            volume = float(raw_market.get("volume", 0))
            
            return {
                "id": market_id,
                "question": question,
                "yes_price": yes_price,
                "no_price": no_price,
                "liquidity": liquidity,
                "volume": volume,
                "hours_to_resolve": hours_to_resolve,
                "end_date": end_date,
                "raw": raw_market
            }
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return {}
    
    def _filter_markets(self, markets: List[Dict]) -> List[Dict]:
        """
        Filter markets by quality criteria:
        - Minimum liquidity
        - Fast resolution preferred
        - High priority keywords
        """
        filtered = []
        
        for market in markets:
            # Skip if no data
            if not market or "id" not in market:
                continue
            
            # Liquidity check
            if market.get("liquidity", 0) < market_config.MIN_MARKET_LIQUIDITY:
                continue
            
            # Resolution time preference
            hours = market.get("hours_to_resolve")
            if hours and hours > market_config.PREFERRED_RESOLUTION_MAX_HOURS:
                continue
            
            # Add priority score
            question = market.get("question", "")
            is_priority = market_config.is_high_priority_market(question)
            market["is_priority"] = is_priority
            
            # Add edge indicators
            yes_price = market.get("yes_price", 0.5)
            no_price = market.get("no_price", 0.5)
            
            # Check for mispricings (YES + NO should equal ~1.0)
            total_prob = yes_price + no_price
            market["mispricing"] = abs(total_prob - 1.0)
            
            filtered.append(market)
        
        # Sort by priority and liquidity
        filtered.sort(
            key=lambda m: (m["is_priority"], m["liquidity"], -m.get("hours_to_resolve", 999)),
            reverse=True
        )
        
        return filtered
    
    def check_liquidity_depth(self, market: Dict) -> Dict[str, bool]:
        """
        Analyze order book depth
        Returns: {"sufficient": bool, "can_fill": bool, "slippage_risk": float}
        """
        liquidity = market.get("liquidity", 0)
        volume = market.get("volume", 0)
        
        # Simple heuristics
        sufficient = liquidity >= market_config.MIN_MARKET_LIQUIDITY
        can_fill = volume > 0 and liquidity > 100
        
        # Estimate slippage risk (0.0 = low, 1.0 = high)
        if liquidity >= market_config.IDEAL_MARKET_LIQUIDITY:
            slippage_risk = 0.1  # Low risk
        elif liquidity >= market_config.MIN_MARKET_LIQUIDITY:
            slippage_risk = 0.3  # Medium risk
        else:
            slippage_risk = 0.8  # High risk
        
        return {
            "sufficient": sufficient,
            "can_fill": can_fill,
            "slippage_risk": slippage_risk
        }
    
    async def place_bet(self, market_id: str, side: str, amount: float, max_price: float) -> bool:
        """
        Execute trade
        Returns: True if successful, False otherwise
        """
        if self.paper_trading:
            logger.info(f"📝 PAPER: {side} ${amount:.2f} @ ${max_price:.3f} on {market_id}")
            return True
        
        if not self.client:
            logger.error("❌ No CLOB client")
            return False
        
        try:
            # Build order
            order = OrderArgs(
                market=market_id,
                side=side.upper(),
                size=str(amount),
                price=str(max_price),
                order_type=OrderType.FOK  # Fill or kill
            )
            
            # Execute
            result = self.client.create_order(order)
            
            logger.info(f"✅ Order placed: {result}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Order failed: {e}")
            return False
    
    async def get_position_value(self, market_id: str, side: str) -> Optional[float]:
        """
        Get current value of position
        Used for exit price monitoring
        """
        try:
            markets = await self._fetch_markets_for_symbol("BTC")  # Refresh
            
            for market in markets:
                if market.get("id") == market_id:
                    if side.upper() == "YES":
                        return market.get("yes_price")
                    else:
                        return market.get("no_price")
            
            return None
        except:
            return None