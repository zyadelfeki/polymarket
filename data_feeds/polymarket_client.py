import asyncio
from typing import Dict, List, Optional
from datetime import datetime
import logging
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from config.settings import settings
from config.markets import CRYPTO_SYMBOLS, PRICE_KEYWORDS

logger = logging.getLogger(__name__)

class PolymarketClient:
    def __init__(self):
        self.host = "https://clob.polymarket.com"
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.chain_id = 137
        self.private_key = settings.POLYMARKET_PRIVATE_KEY
        
        if not self.private_key or self.private_key == "":
            logger.warning("No private key configured - read-only mode")
            self.client = ClobClient(self.host)
            self.address = None
            self.can_trade = False
        else:
            try:
                self.client = ClobClient(
                    self.host,
                    key=self.private_key,
                    chain_id=self.chain_id
                )
                
                if not settings.PAPER_TRADING:
                    api_creds = self.client.create_or_derive_api_creds()
                    self.client.set_api_creds(api_creds)
                    logger.info("API credentials configured")
                
                from eth_account import Account
                account = Account.from_key(self.private_key)
                self.address = account.address
                self.can_trade = True
                logger.info(f"Polymarket client initialized: {self.address}")
                
            except Exception as e:
                logger.error(f"Failed to initialize Polymarket client: {e}")
                self.client = ClobClient(self.host)
                self.address = None
                self.can_trade = False
        
        self.market_cache = {}
        self.last_market_refresh = None
    
    async def get_markets(self, active: bool = True, limit: int = 100) -> List[Dict]:
        try:
            markets = self.client.get_markets()
            
            if active:
                markets = [m for m in markets if not m.get("closed", False) and m.get("active", True)]
            
            markets = markets[:limit]
            
            self.market_cache = {m.get("condition_id", m.get("id")): m for m in markets}
            self.last_market_refresh = datetime.utcnow()
            
            return markets
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
        try:
            orderbook = self.client.get_order_book(token_id)
            return orderbook
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
        if orders and len(orders) > 0:
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
            import time
            return {"success": True, "order_id": f"paper_{int(time.time())}", "paper": True}
        
        if not self.can_trade:
            logger.error("Cannot trade - no private key or initialization failed")
            return None
        
        try:
            order_side = BUY if side.upper() == "BUY" else SELL
            
            order = OrderArgs(
                token_id=token_id,
                price=price,
                size=amount,
                side=order_side
            )
            
            signed_order = self.client.create_order(order)
            response = self.client.post_order(signed_order, OrderType.GTC)
            
            logger.info(f"Order placed: {response.get('orderID', 'unknown')}")
            return {"success": True, "order_id": response.get('orderID'), "response": response}
            
        except Exception as e:
            logger.error(f"Order execution error: {e}", exc_info=True)
            return None
    
    def close(self):
        pass