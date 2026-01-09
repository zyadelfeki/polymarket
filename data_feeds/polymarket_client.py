import aiohttp
import asyncio
from typing import Dict, List, Optional
import time
import hmac
import hashlib
from web3 import Web3
from eth_account import Account
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class PolymarketCLOBClient:
    def __init__(self):
        self.base_url = "https://clob.polymarket.com"
        self.api_key = settings.POLYMARKET_API_KEY
        self.api_secret = settings.POLYMARKET_SECRET
        self.private_key = settings.POLYMARKET_PRIVATE_KEY
        self.w3 = Web3(Web3.HTTPProvider(settings.POLYGON_RPC_URL))
        if self.private_key:
            self.account = Account.from_key(self.private_key)
        else:
            self.account = None
        self.markets_cache = {}
        self.cache_time = 0
    
    def _sign_message(self, message: str) -> str:
        if not self.api_secret:
            return ""
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    async def get_markets(self, closed: bool = False, limit: int = 100) -> List[Dict]:
        url = f"{self.base_url}/markets"
        params = {"closed": str(closed).lower(), "limit": limit}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data
                    else:
                        logger.error(f"Failed to fetch markets: {response.status}")
                        return []
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    async def get_active_crypto_markets(self) -> List[Dict]:
        all_markets = await self.get_markets(closed=False, limit=200)
        crypto_keywords = ['btc', 'bitcoin', 'eth', 'ethereum', 'sol', 'solana', 'crypto']
        crypto_markets = []
        for market in all_markets:
            question = market.get('question', '').lower()
            if any(kw in question for kw in crypto_keywords):
                crypto_markets.append(market)
        logger.info(f"Found {len(crypto_markets)} active crypto markets")
        return crypto_markets
    
    async def get_market_orderbook(self, token_id: str) -> Dict:
        url = f"{self.base_url}/book"
        params = {"token_id": token_id}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=5) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        return {"bids": [], "asks": []}
        except Exception as e:
            logger.error(f"Error fetching orderbook: {e}")
            return {"bids": [], "asks": []}
    
    async def get_market_price(self, market_id: str, side: str = "YES") -> Optional[float]:
        try:
            markets = await self.get_markets()
            for market in markets:
                if market.get('condition_id') == market_id:
                    tokens = market.get('tokens', [])
                    if side.upper() == "YES" and len(tokens) > 0:
                        return float(tokens[0].get('price', 0))
                    elif side.upper() == "NO" and len(tokens) > 1:
                        return float(tokens[1].get('price', 0))
            return None
        except Exception as e:
            logger.error(f"Error getting market price: {e}")
            return None
    
    async def place_order(self, market_id: str, side: str, amount: float, price: float) -> Dict:
        if settings.PAPER_TRADING:
            logger.info(f"[PAPER] Order: {side} ${amount:.2f} at ${price:.3f} on {market_id}")
            return {"success": True, "paper_trade": True, "order_id": f"paper_{int(time.time())}"}
        
        logger.warning("Live trading not implemented - add your CLOB order logic here")
        return {"success": False, "error": "Live trading requires full CLOB integration"}
    
    async def place_bet(self, market_id: str, side: str, amount: float, max_price: float) -> bool:
        result = await self.place_order(market_id, side, amount, max_price)
        return result.get("success", False)
    
    async def sell_position(self, market_id: str, side: str, amount: float, min_price: float) -> bool:
        opposite_side = "NO" if side == "YES" else "YES"
        result = await self.place_order(market_id, opposite_side, amount, min_price)
        return result.get("success", False)
    
    async def scan_markets_parallel(self, symbols: List[str], binance_prices: Dict) -> List[Dict]:
        markets = await self.get_active_crypto_markets()
        opportunities = []
        
        for market in markets:
            question = market.get('question', '').lower()
            tokens = market.get('tokens', [])
            
            if not tokens or len(tokens) < 2:
                continue
            
            yes_price = float(tokens[0].get('price', 0.5))
            no_price = float(tokens[1].get('price', 0.5))
            
            for symbol in symbols:
                if symbol.lower() in question:
                    binance_price = binance_prices.get(symbol, {}).get('price', 0)
                    if binance_price == 0:
                        continue
                    
                    threshold = self._extract_threshold_from_question(question)
                    if threshold is None:
                        continue
                    
                    true_outcome = "YES" if binance_price > threshold else "NO"
                    market_price = yes_price if true_outcome == "YES" else no_price
                    edge = (1.0 - market_price) if market_price < 0.90 else 0
                    
                    if edge > 0.10:
                        opportunities.append({
                            "market_id": market.get('condition_id'),
                            "question": market.get('question'),
                            "symbol": symbol,
                            "binance_price": binance_price,
                            "threshold": threshold,
                            "true_outcome": true_outcome,
                            "market_price": market_price,
                            "edge": edge,
                            "confidence": 0.95 if edge > 0.20 else 0.85
                        })
        
        opportunities.sort(key=lambda x: x['edge'], reverse=True)
        return opportunities
    
    def _extract_threshold_from_question(self, question: str) -> Optional[float]:
        import re
        patterns = [
            r'above \$?([0-9,]+)',
            r'over \$?([0-9,]+)',
            r'reach \$?([0-9,]+)',
            r'\$([0-9,]+) or more'
        ]
        for pattern in patterns:
            match = re.search(pattern, question, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1).replace(',', ''))
                except:
                    continue
        return None