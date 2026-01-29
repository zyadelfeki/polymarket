import asyncio
import os
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging
import json
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
    _clob_available = True
except ImportError:
    ClobClient = None
    OrderArgs = None
    OrderType = None
    BUY = None
    SELL = None
    _clob_available = False
from config.settings import settings
from config.markets import CRYPTO_SYMBOLS, PRICE_KEYWORDS

logger = logging.getLogger(__name__)

class PolymarketClient:
    def __init__(self):
        self.host = "https://clob.polymarket.com"
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.chain_id = 137
        self.private_key = settings.POLYMARKET_PRIVATE_KEY
        
        if not _clob_available:
            logger.error("Polymarket SDK not available - running in read-only mode")
            self.client = None
            self.address = None
            self.can_trade = False
            self.market_cache = {}
            self.last_market_refresh = None
            return

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
        self._paper_orders: Dict[str, Dict[str, float]] = {}

        # Auth retry + key rotation
        self.auth_retry_count = 0
        self.max_auth_retries = 3
        self.api_key_rotation_enabled = os.getenv("API_KEY_ROTATION", "false").lower() == "true"
        self.backup_api_keys: List[Dict[str, str]] = self._load_backup_api_keys()
        self._active_backup_key_index = 0
        self.emergency_shutdown_reason: Optional[str] = None

    def _load_backup_api_keys(self) -> List[Dict[str, str]]:
        """Load backup API keys from environment as JSON array."""
        raw = os.getenv("POLYMARKET_BACKUP_API_KEYS", "")
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                keys: List[Dict[str, str]] = []
                for entry in data:
                    if isinstance(entry, dict):
                        if entry.get("api_key") and entry.get("secret") and entry.get("passphrase"):
                            keys.append(entry)
                return keys
            return []
        except Exception:
            logger.warning("backup_api_keys_parse_failed")
            return []

    def _rotate_api_key(self) -> bool:
        """Rotate to the next API key in the backup list."""
        if not self.backup_api_keys or not self.client:
            return False
        self._active_backup_key_index = (self._active_backup_key_index + 1) % len(self.backup_api_keys)
        creds = self.backup_api_keys[self._active_backup_key_index]
        try:
            if hasattr(self.client, "set_api_creds"):
                self.client.set_api_creds(creds)
                logger.warning("api_key_rotated", extra={"index": self._active_backup_key_index})
                return True
        except Exception as e:
            logger.error(f"API key rotation failed: {e}")
        return False

    @staticmethod
    def _extract_http_status(error: Exception) -> Optional[int]:
        for attr in ("status_code", "status", "code"):
            if hasattr(error, attr):
                try:
                    value = getattr(error, attr)
                    if isinstance(value, int):
                        return value
                except Exception:
                    continue
        message = str(error).lower()
        if "401" in message:
            return 401
        if "403" in message:
            return 403
        return None

    async def _handle_auth_error(self, error_code: int) -> bool:
        """Handle 401/403 with exponential backoff and optional key rotation."""
        self.auth_retry_count += 1
        if error_code == 401:
            logger.error("API key invalid - attempting rotation")
            if self.api_key_rotation_enabled and self.backup_api_keys:
                if self._rotate_api_key():
                    return True
        if self.auth_retry_count >= self.max_auth_retries:
            await self._emergency_shutdown("AUTH_FAILURE_CRITICAL")
            return False
        await asyncio.sleep(2 ** self.auth_retry_count)
        return True

    async def _emergency_shutdown(self, reason: str) -> None:
        """Disable trading on critical auth failures."""
        self.can_trade = False
        self.emergency_shutdown_reason = reason
        logger.critical(f"Emergency shutdown: {reason}")
    
    async def get_markets(self, active: bool = True, limit: int = 100) -> List[Dict]:
        try:
            markets = self.client.get_markets()

            if isinstance(markets, dict):
                markets = markets.get("data") or markets.get("markets") or []

            if not isinstance(markets, list):
                logger.warning("Unexpected markets response type")
                markets = []

            markets = [m for m in markets if isinstance(m, dict)]
            
            if active:
                markets = [m for m in markets if not m.get("closed", False) and m.get("active", True)]
            
            markets = markets[:limit]
            
            self.market_cache = {m.get("condition_id", m.get("id")): m for m in markets}
            self.last_market_refresh = datetime.utcnow()
            
            return markets
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []

    async def get_market(self, market_id: str) -> Optional[Dict]:
        """Fetch a single market by condition_id, with Gamma fallback."""
        try:
            if hasattr(self.client, "get_market"):
                market = await asyncio.to_thread(self.client.get_market, market_id)
                if isinstance(market, dict):
                    return market
            return await self._fetch_market_via_gamma(market_id)
        except Exception as e:
            logger.error(f"Error fetching market {market_id}: {e}")
            return None

    async def _fetch_market_via_gamma(self, condition_id: str) -> Optional[Dict]:
        try:
            import httpx

            base_url = "https://gamma-api.polymarket.com/markets"
            candidates = [
                f"{base_url}?condition_ids={condition_id}",
                f"{base_url}?condition_id={condition_id}",
                f"{base_url}?conditionId={condition_id}",
            ]
            async with httpx.AsyncClient(timeout=10.0) as client:
                for url in candidates:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    if isinstance(data, dict) and data.get("data"):
                        items = data.get("data")
                        if isinstance(items, list) and items:
                            return items[0]
            return None
        except Exception as e:
            logger.error(f"Gamma market lookup failed: {e}")
            return None
    
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

    async def get_open_positions(self) -> List[Dict[str, Any]]:
        """Get open positions from the exchange if supported."""
        if settings.PAPER_TRADING:
            return []
        if not self.can_trade:
            logger.error("Cannot fetch positions - not authenticated")
            return []

        try:
            if hasattr(self.client, "get_positions"):
                positions = await asyncio.to_thread(self.client.get_positions)
            elif hasattr(self.client, "list_positions"):
                positions = await asyncio.to_thread(self.client.list_positions)
            else:
                logger.warning("Positions endpoint not available in client")
                return []
            if isinstance(positions, dict):
                positions = positions.get("data") or positions.get("positions") or []
            if not isinstance(positions, list):
                return []
            return positions
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []
    
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
            order_id = f"paper_{int(time.time())}"
            self._paper_orders[order_id] = {"price": price, "amount": amount}
            return {"success": True, "order_id": order_id, "paper": True}
        
        if not self.can_trade or not _clob_available:
            logger.error("Cannot trade - no private key or initialization failed")
            return None
        
        for _ in range(self.max_auth_retries):
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
                self.auth_retry_count = 0
                return {"success": True, "order_id": response.get('orderID'), "response": response}

            except Exception as e:
                status_code = self._extract_http_status(e)
                if status_code in (401, 403):
                    should_retry = await self._handle_auth_error(status_code)
                    if should_retry:
                        continue
                    return None
                logger.error(f"Order execution error: {e}", exc_info=True)
                return None
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        if settings.PAPER_TRADING:
            self._paper_orders.pop(order_id, None)
            return True
        if not self.can_trade or not self.client:
            logger.error("Cannot cancel order - not authenticated")
            return False
        try:
            if hasattr(self.client, "cancel"):
                await asyncio.to_thread(self.client.cancel, order_id)
            elif hasattr(self.client, "cancel_order"):
                await asyncio.to_thread(self.client.cancel_order, order_id)
            else:
                logger.warning("Cancel order not supported by client")
                return False
            self.auth_retry_count = 0
            return True
        except Exception as e:
            status_code = self._extract_http_status(e)
            if status_code in (401, 403):
                await self._handle_auth_error(status_code)
            logger.error(f"Order cancellation error: {e}", exc_info=True)
            return False

    async def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order status for an order ID."""
        if settings.PAPER_TRADING:
            order = self._paper_orders.get(order_id)
            if not order:
                return {"status": "CANCELLED"}
            return {
                "status": "MATCHED",
                "filled_price": order.get("price"),
                "filled_quantity": order.get("amount"),
                "fees": 0,
            }
        if not self.can_trade or not self.client:
            logger.error("Cannot fetch order status - not authenticated")
            return None
        try:
            if hasattr(self.client, "get_order"):
                response = await asyncio.to_thread(self.client.get_order, order_id)
            elif hasattr(self.client, "get_order_status"):
                response = await asyncio.to_thread(self.client.get_order_status, order_id)
            else:
                logger.warning("Order status endpoint not available in client")
                return None

            if not isinstance(response, dict):
                return None

            status = response.get("status") or response.get("state") or response.get("order_status")
            status = (status or "").upper()
            if status in {"FILLED", "MATCHED", "EXECUTED"}:
                mapped_status = "MATCHED"
            elif status in {"CANCELLED", "CANCELED"}:
                mapped_status = "CANCELLED"
            elif status in {"FAILED", "REJECTED"}:
                mapped_status = "FAILED"
            else:
                mapped_status = status or "OPEN"

            fills = response.get("fills") or []
            filled_price = response.get("filled_price") or response.get("price")
            filled_quantity = response.get("filled_quantity") or response.get("size")
            fees = response.get("fees") or response.get("fee") or 0

            if fills and (filled_price is None or filled_quantity is None):
                try:
                    last_fill = fills[-1]
                    filled_price = filled_price or last_fill.get("price")
                    filled_quantity = filled_quantity or last_fill.get("size")
                    fees = fees or last_fill.get("fee") or 0
                except Exception:
                    pass

            self.auth_retry_count = 0
            return {
                "status": mapped_status,
                "filled_price": filled_price,
                "filled_quantity": filled_quantity,
                "fees": fees,
                "raw": response,
            }
        except Exception as e:
            status_code = self._extract_http_status(e)
            if status_code in (401, 403):
                await self._handle_auth_error(status_code)
            logger.error(f"Order status error: {e}", exc_info=True)
            return None
    
    def close(self):
        pass