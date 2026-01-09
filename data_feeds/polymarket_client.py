import asyncio
import aiohttp
import time
import hmac
import hashlib
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import logging
from web3 import Web3
from eth_account import Account
from config.settings import settings
from config.markets import CRYPTO_SYMBOLS, PRICE_KEYWORDS

logger = logging.getLogger(__name__)

class PolymarketClient:
    def __init__(self):
        self.base_url = "https://clob.polymarket.com"
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.private_key = settings.POLYMARKET_PRIVATE_KEY
        
        if self.private_key:
            self.account = Account.from_key(self.private_key)
            self.address = self.account.address
        else:
            self.account = None
            self.address = None
        
        self.session = None
        self.market_cache = {}
        self.last_market_refresh = None
    
    async def _get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    def _sign_message(self, message: str) -> str:
        if not self.account:
            return ""
        message_hash = Web3.keccak(text=message)
        signed = self.account.signHash(message_hash)
        return signed.signature.hex()
    
    async def get_markets(self, active: bool = True, closed: bool = False) -> List[Dict]:
        session = await self._get_session()
        
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": 100
        }
        
        try:
            async with session.get(f"{self.gamma_url}/markets", params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    self.market_cache = {m.get("condition_id", m.get("id")): m for m in markets}
                    self.last_market_refresh = datetime.utcnow()
                    return markets
                else:
                    logger.error(f"Failed to fetch markets: {response.status}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    async def scan_crypto_markets_parallel(self, symbols: List[str] = None) -> List[Dict]:
        if symbols is None:
            symbols = list(CRYPTO_SYMBOLS.keys())
        
        markets = await self.get_markets()
        
        crypto_markets = []
        for market in markets:
            question = market.get("question", "").lower()
            description = market.get("description", "").lower()
            
            for symbol in symbols:
                tags = CRYPTO_SYMBOLS[symbol]["polymarket_tags"]
                if any(tag in question or tag in description for tag in tags):
                    if any(keyword in question for keyword in PRICE_KEYWORDS):
                        crypto_markets.append(market)
                        break
        
        logger.info(f"Found {len(crypto_markets)} active crypto price markets")
        return crypto_markets
    
    async def get_market_orderbook(self, token_id: str) -> Dict:
        session = await self._get_session()
        
        try:
            async with session.get(f"{self.base_url}/book", params={"token_id": token_id}, timeout=5) as response:
                if response.status == 200:
                    return await response.json()
                return {"bids": [], "asks": []}
        except Exception as e:
            logger.error(f"Error fetching orderbook for {token_id}: {e}")
            return {"bids": [], "asks": []}
    
    async def get_market_prices_parallel(self, markets: List[Dict]) -> Dict[str, Dict]:
        tasks = []
        for market in markets:
            condition_id = market.get("condition_id", market.get("id"))
            tokens = market.get("tokens", [])
            
            if len(tokens) >= 2:
                yes_token = tokens[0].get("token_id")
                no_token = tokens[1].get("token_id")
                
                if yes_token and no_token:
                    tasks.append(self._fetch_market_price(condition_id, yes_token, no_token, market))
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        price_map = {}
        for result in results:
            if isinstance(result, dict) and "condition_id" in result:
                price_map[result["condition_id"]] = result
        
        return price_map
    
    async def _fetch_market_price(self, condition_id: str, yes_token: str, no_token: str, market: Dict) -> Dict:
        yes_book, no_book = await asyncio.gather(
            self.get_market_orderbook(yes_token),
            self.get_market_orderbook(no_token),
            return_exceptions=True
        )
        
        yes_price = self._get_best_price(yes_book, "ask") if isinstance(yes_book, dict) else 0.50
        no_price = self._get_best_price(no_book, "ask") if isinstance(no_book, dict) else 0.50
        
        yes_liquidity = self._calculate_liquidity(yes_book) if isinstance(yes_book, dict) else 0
        no_liquidity = self._calculate_liquidity(no_book) if isinstance(no_book, dict) else 0
        
        return {
            "condition_id": condition_id,
            "market_title": market.get("question", ""),
            "yes_price": yes_price,
            "no_price": no_price,
            "yes_token": yes_token,
            "no_token": no_token,
            "total_liquidity": yes_liquidity + no_liquidity,
            "yes_liquidity": yes_liquidity,
            "no_liquidity": no_liquidity,
            "end_date": market.get("end_date_iso"),
            "market_data": market
        }
    
    def _get_best_price(self, orderbook: Dict, side: str) -> float:
        orders = orderbook.get("asks" if side == "ask" else "bids", [])
        if orders:
            return float(orders[0].get("price", 0.50))
        return 0.50
    
    def _calculate_liquidity(self, orderbook: Dict) -> float:
        total = 0.0
        for side in ["asks", "bids"]:
            for order in orderbook.get(side, [])[:10]:
                total += float(order.get("size", 0)) * float(order.get("price", 0))
        return total
    
    async def place_order(self, token_id: str, side: str, amount: float, price: float) -> Optional[Dict]:
        if settings.PAPER_TRADING:
            logger.info(f"[PAPER] Order: {side} {amount:.2f} shares @ ${price:.3f}")
            return {"success": True, "order_id": f"paper_{int(time.time())}", "paper": True}
        
        if not self.account:
            logger.error("No private key configured for real trading")
            return None
        
        session = await self._get_session()
        
        order_data = {
            "token_id": token_id,
            "price": str(price),
            "size": str(amount),
            "side": side.upper(),
            "maker": self.address
        }
        
        try:
            async with session.post(f"{self.base_url}/order", json=order_data, timeout=settings.EXECUTION_TIMEOUT_SEC) as response:
                if response.status in [200, 201]:
                    result = await response.json()
                    logger.info(f"Order placed: {result.get('order_id')}")
                    return result
                else:
                    error_text = await response.text()
                    logger.error(f"Order failed: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            return None
    
    async def close(self):
        if self.session:
            await self.session.close()