#!/usr/bin/env python3
"""
Polymarket Client V2 - Nuclear Authentication Fix + Web3 Balance Check

This version forces synchronous authentication in __init__.
No lazy initialization. No deferred credential derivation.

Uses Web3 to check Proxy Wallet balance directly instead of ClobClient.
"""

import os
import asyncio
import time
import json
from decimal import Decimal
from typing import Optional, Dict, List, Any
from enum import Enum
try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

try:
    from web3 import Web3
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
    from py_clob_client.order_builder.constants import BUY, SELL
    from eth_account import Account
    POLYMARKET_AVAILABLE = True
except ImportError:
    Web3 = None
    ClobClient = None
    OrderArgs = None
    OrderType = None
    BalanceAllowanceParams = None
    BUY = None
    SELL = None
    Account = None
    POLYMARKET_AVAILABLE = False

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

    logger = _FallbackLogger(__name__)


class OrderSide(Enum):
    """Order side enum"""
    BUY = "BUY"
    SELL = "SELL"


class PolymarketClientV2:
    """
    Simplified Polymarket client with FORCED authentication in __init__.
    
    The bot WILL authenticate immediately when constructed with a private key.
    No async initialization needed. No lazy loading. Just works.
    """
    
    def __init__(
        self,
        private_key: Optional[str] = None,
        api_key: Optional[str] = None,
        rate_limit: float = 8.0,
        max_retries: int = 3,
        timeout: float = 10.0,
        paper_trading: bool = True
    ):
        """
        Initialize client with IMMEDIATE authentication.
        
        Args:
            private_key: Ethereum private key (0x...)
            api_key: API key (optional, will be derived)
            rate_limit: Max requests per second
            max_retries: Max retry attempts
            timeout: Request timeout in seconds
            paper_trading: If True, simulate trades
        """
        self.host = "https://clob.polymarket.com"
        self.chain_id = 137  # Polygon Mainnet
        
        self.private_key = private_key
        self.paper_trading = paper_trading
        self.max_retries = max_retries
        self.timeout = timeout
        self.rate_limit = max(rate_limit, 0.1)
        
        # Load proxy address from environment
        self.proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        
        # Initialize Web3 for balance checks (optional)
        if Web3:
            self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        else:
            self.w3 = None
        
        # State tracking
        self.client: Optional[Any] = None
        self.address: Optional[str] = None
        self.can_trade = False
        self.authenticated = False
        self._rate_lock = asyncio.Lock()
        self._last_request_ts = 0.0
        
        # Initialize client NOW (synchronously)
        self._force_authentication()
        
        logger.info(
            "polymarket_client_initialized",
            address=self.address,
            proxy_address=self.proxy_address,
            authenticated=self.authenticated,
            can_trade=self.can_trade,
            paper_trading=self.paper_trading,
            has_client=bool(self.client),
            web3_connected=self.w3.is_connected() if self.w3 else False
        )
    
    def _force_authentication(self):
        """
        FORCE authentication to happen NOW in __init__.
        
        This is synchronous and blocks until complete.
        No lazy initialization. No deferred credentials.
        """
        if not POLYMARKET_AVAILABLE:
            logger.error("polymarket_sdk_not_available", message="Install py-clob-client")
            self.client = None
            self.can_trade = False
            self.authenticated = False
            return
        
        if not self.private_key or self.private_key == "your_private_key_here":
            logger.warning("no_private_key_provided", mode="read_only")
            # Create read-only client
            try:
                self.client = ClobClient(self.host)
            except Exception as e:
                logger.error("failed_to_create_readonly_client", error=str(e))
            return
        
        try:
            logger.info("starting_forced_authentication", paper_trading=self.paper_trading)
            
            # Step 1: Create client with private key
            logger.debug("creating_clob_client_with_key")
            self.client = ClobClient(
                host=self.host,
                key=self.private_key,
                chain_id=self.chain_id
            )
            
            # Step 2: Derive wallet address
            account = Account.from_key(self.private_key)
            self.address = account.address
            logger.info("wallet_address_derived", address=self.address)
            
            # Step 3: For LIVE mode, derive API credentials NOW
            if not self.paper_trading:
                logger.info("live_mode_deriving_api_credentials_NOW")
                
                # CRITICAL: Call create_or_derive_api_key synchronously
                try:
                    logger.debug("calling_create_or_derive_api_key")
                    creds = self.client.create_or_derive_api_key()
                    
                    logger.info(
                        "api_key_derived_successfully",
                        api_key_prefix=creds.api_key[:8] + "..." if hasattr(creds, 'api_key') else "unknown",
                        has_secret=bool(hasattr(creds, 'secret')),
                        has_passphrase=bool(hasattr(creds, 'passphrase'))
                    )
                    
                    # Step 4: Reinitialize client WITH credentials
                    logger.debug("reinitializing_client_with_credentials")
                    self.client = ClobClient(
                        host=self.host,
                        key=self.private_key,
                        chain_id=self.chain_id,
                        creds=creds
                    )
                    
                    self.authenticated = True
                    logger.info("clob_client_authenticated_successfully", address=self.address)
                    
                except AttributeError:
                    # Fallback: try create_or_derive_api_creds
                    logger.warning("create_or_derive_api_key_not_found_trying_alt_method")
                    try:
                        api_creds_dict = self.client.create_or_derive_api_creds()
                        self.client.set_api_creds(api_creds_dict)
                        self.authenticated = True
                        logger.info("clob_client_authenticated_via_alt_method", address=self.address)
                    except Exception as e:
                        logger.error("alt_auth_method_failed", error=str(e), error_type=type(e).__name__)
                        self.authenticated = False
                
                except Exception as e:
                    logger.error(
                        "api_credential_derivation_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        address=self.address
                    )
                    self.authenticated = False
            else:
                # Paper trading: no auth needed
                logger.info("paper_trading_mode_no_auth_needed")
                self.authenticated = True  # "Authenticated" for paper trading purposes
            
            # Mark as ready to trade
            self.can_trade = True
            
        except Exception as e:
            logger.error(
                "client_initialization_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            self.client = None
            self.can_trade = False
            self.authenticated = False

    async def _throttle(self):
        """Simple rate limiter to respect max requests per second."""
        if self.rate_limit <= 0:
            return
        min_interval = 1.0 / self.rate_limit
        async with self._rate_lock:
            now = time.time()
            wait = min_interval - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.time()
    
    async def initialize(self) -> bool:
        """
        Async initialization (kept for compatibility, but does nothing now).
        
        Authentication already happened in __init__.
        
        Returns:
            True if client is ready
        """
        logger.debug(
            "initialize_called_but_already_done",
            authenticated=self.authenticated,
            can_trade=self.can_trade
        )
        return self.authenticated
    
    async def get_usdc_balance(self) -> Decimal:
        """
        Fetch USDC balance from Polymarket Proxy Wallet using Web3.
        
        Checks BOTH Bridged USDC and Native USDC contracts on Polygon.
        
        Returns:
            Total USDC balance as Decimal
        """
        logger.debug(
            "get_usdc_balance_called",
            paper_trading=self.paper_trading,
            proxy_address=self.proxy_address,
            has_web3=bool(self.w3)
        )
        
        # Paper trading: return 0
        if self.paper_trading:
            logger.debug("paper_trading_balance_is_zero")
            return Decimal('0')
        
        # Check proxy address
        if not self.proxy_address:
            logger.error("no_proxy_address", message="Set POLYMARKET_PROXY_ADDRESS env variable")
            return Decimal('0')
        
        # Check Web3 connection
        if not self.w3:
            logger.error("no_web3_connection")
            return Decimal('0')
        
        try:
            logger.debug("fetching_balance_via_web3_dual_usdc_check")
            
            # Define BOTH USDC contract addresses on Polygon
            BRIDGED_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            
            # Minimal ERC20 ABI for balanceOf
            usdc_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                }
            ]
            
            # Create contract instances for BOTH
            bridged_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(BRIDGED_USDC),
                abi=usdc_abi
            )
            native_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(NATIVE_USDC),
                abi=usdc_abi
            )
            
            # Get proxy address in checksum format
            proxy_checksum = Web3.to_checksum_address(self.proxy_address)
            
            # Fetch balance from BOTH contracts
            loop = asyncio.get_running_loop()
            
            balance_bridged = await loop.run_in_executor(
                None,
                lambda: bridged_contract.functions.balanceOf(proxy_checksum).call()
            )
            
            balance_native = await loop.run_in_executor(
                None,
                lambda: native_contract.functions.balanceOf(proxy_checksum).call()
            )
            
            # Sum the balances
            total_raw = balance_bridged + balance_native
            
            # Convert from atomic units (6 decimals) to USDC
            total_decimal = Decimal(str(total_raw)) / Decimal("1000000")
            bridged_decimal = Decimal(str(balance_bridged)) / Decimal("1000000")
            native_decimal = Decimal(str(balance_native)) / Decimal("1000000")
            
            logger.info(
                "balance_check",
                proxy=self.proxy_address,
                bridged=float(bridged_decimal),
                native=float(native_decimal),
                total=float(total_decimal)
            )
            
            return total_decimal
            
        except Exception as e:
            logger.error(
                "web3_balance_fetch_failed",
                error=str(e),
                error_type=type(e).__name__,
                proxy=self.proxy_address,
                exc_info=True
            )
            return Decimal('0')
    
    async def get_market(self, market_id: str) -> Optional[Dict]:
        """
        Get market information using direct lookup.
        
        Uses get_market() for direct condition_id lookup instead of 
        iterating through get_markets() pagination.
        
        Args:
            market_id: Market condition_id (0x...)
        
        Returns:
            Market dict or safe default with tokens field
        """
        if self.paper_trading or not self.client:
            # Return mock data for paper trading
            logger.debug("returning_mock_market_data", market_id=market_id)
            return {
                "market_id": market_id,
                "question": f"Mock market: {market_id}",
                "tokens": [
                    {"token_id": "yes", "outcome": "YES", "price": 0.50},
                    {"token_id": "no", "outcome": "NO", "price": 0.50}
                ],
                "yes_price": 0.50,
                "no_price": 0.50,
                "yes_token_id": "yes",
                "no_token_id": "no",
                "mock": True
            }
        
        try:
            await self._throttle()
            logger.debug("fetching_market_via_direct_lookup", market_id=market_id)
            market = None
            if hasattr(self.client, "get_market"):
                loop = asyncio.get_running_loop()
                market = await loop.run_in_executor(
                    None,
                    lambda: self.client.get_market(market_id)
                )
            
            logger.debug("direct_market_lookup_response_type", type=type(market).__name__)
            
            # Validate response
            if not isinstance(market, dict):
                logger.warning("market_response_not_dict", type=type(market).__name__)
                market = None
            
            # Fallback to Gamma API if needed
            if not market or 'tokens' not in market:
                market = await self._fetch_market_via_gamma(market_id)
            if not market or 'tokens' not in market:
                logger.warning("market_missing_tokens_field", market_id=market_id)
                return {"tokens": [], "error": "market_not_found", "market_id": market_id}

            yes_token, no_token = self._infer_yes_no_tokens(market.get('tokens', []))
            if not yes_token or not no_token:
                logger.warning("unable_to_infer_yes_no_tokens", market_id=market_id)
                return {"tokens": market.get('tokens', []), "error": "token_inference_failed", "market_id": market_id}

            yes_token_id = yes_token.get('token_id') or yes_token.get('tokenId')
            no_token_id = no_token.get('token_id') or no_token.get('tokenId')

            yes_price = await self._get_best_ask(yes_token_id)
            no_price = await self._get_best_ask(no_token_id)

            if yes_price is None:
                yes_price = self._extract_token_price(yes_token)
            if no_price is None:
                no_price = self._extract_token_price(no_token)

            market['yes_price'] = yes_price
            market['no_price'] = no_price
            market['yes_token_id'] = yes_token_id
            market['no_token_id'] = no_token_id
            
            logger.info(
                "market_found_via_direct_lookup",
                market_id=market_id,
                question=market.get('question', 'N/A')[:50],
                has_tokens=bool(market.get('tokens')),
                token_count=len(market.get('tokens', []))
            )
            
            return market
            
        except AttributeError as e:
            # Method doesn't exist - fallback not implemented
            logger.error(
                "get_market_method_not_available",
                error=str(e),
                market_id=market_id,
                message="get_market() not found in ClobClient"
            )
            return {"tokens": [], "error": "method_not_available", "market_id": market_id}
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Check for 404 / not found errors
            if '404' in error_str or 'not found' in error_str:
                logger.warning(
                    "market_not_found_via_direct_lookup",
                    market_id=market_id,
                    error=str(e)
                )
            else:
                logger.error(
                    "market_fetch_failed",
                    market_id=market_id,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True
                )
            
            # Return safe default
            return {"tokens": [], "error": str(e), "market_id": market_id}

    async def _fetch_market_via_gamma(self, condition_id: str) -> Optional[Dict]:
        """Fallback market lookup using Gamma API by condition id."""
        try:
            import httpx

            await self._throttle()
            base_url = "https://gamma-api.polymarket.com/markets"
            candidates = [
                f"{base_url}?condition_ids={condition_id}",
                f"{base_url}?condition_id={condition_id}",
                f"{base_url}?conditionId={condition_id}"
            ]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for url in candidates:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    if isinstance(data, dict) and data.get('data'):
                        items = data.get('data')
                        if isinstance(items, list) and items:
                            return items[0]
            return None
        except Exception as e:
            logger.error("gamma_market_lookup_failed", error=str(e), condition_id=condition_id)
            return None

    def _infer_yes_no_tokens(self, tokens: List[Dict]) -> tuple:
        """Infer YES/NO tokens from token metadata."""
        yes_token = None
        no_token = None
        for token in tokens:
            label = (
                str(token.get('outcome') or token.get('label') or token.get('name') or token.get('symbol') or "")
                .strip()
                .lower()
            )
            if label == 'yes':
                yes_token = token
            elif label == 'no':
                no_token = token
        if not yes_token or not no_token:
            if len(tokens) >= 2:
                yes_token = yes_token or tokens[0]
                no_token = no_token or tokens[1]
        return yes_token, no_token

    def _extract_token_price(self, token: Dict) -> Optional[float]:
        price = token.get('price')
        if price is None:
            price = token.get('last_price')
        try:
            return float(price) if price is not None else None
        except Exception:
            return None

    async def _get_best_ask(self, token_id: Optional[str]) -> Optional[float]:
        if not token_id:
            return None
        await self._throttle()
        orderbook = await self.get_orderbook(token_id)
        if not orderbook:
            return None
        asks = orderbook.get("asks", [])
        if asks:
            try:
                return float(asks[0].get("price"))
            except Exception:
                return None
        return None
    
    async def place_order(
        self,
        token_id: str,
        side: Any,
        price: float,
        size: float,
        order_type: str = "GTC",
        market_id: Optional[str] = None
    ) -> Dict:
        """
        Place an order.
        
        Args:
            market_id: Market ID
            token_id: Token ID
            side: 'BUY' or 'SELL'
            price: Limit price (0.01-0.99)
            size: Order size
        
        Returns:
            Dict with success flag and order_id
        """
        # Paper trading simulation
        await self._throttle()

        if self.paper_trading:
            order_id = f"paper_{int(time.time() * 1000)}"
            logger.info(
                "paper_order_placed",
                order_id=order_id,
                market=market_id,
                token=token_id,
                side=side,
                price=price,
                size=size
            )
            return {"success": True, "order_id": order_id, "paper": True}
        
        # Check authentication for live trading
        if not self.authenticated or not self.client:
            logger.error("cannot_place_order", reason="not_authenticated")
            return {"success": False, "error": "not_authenticated"}
        
        # Validate inputs
        if not (0.01 <= float(price) <= 0.99):
            logger.error("invalid_price", price=price, valid_range="0.01-0.99")
            return {"success": False, "error": "invalid_price"}
        
        if float(size) <= 0:
            logger.error("invalid_size", size=size)
            return {"success": False, "error": "invalid_size"}
        
        try:
            logger.info(
                "placing_live_order",
                market=market_id,
                token=token_id,
                side=side,
                price=price,
                size=size
            )
            
            # Create order
            side_str = side.value if isinstance(side, OrderSide) else str(side)
            order_side = SELL if side_str.upper() == "SELL" else BUY
            order = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=order_side
            )
            
            # Sign and post
            loop = asyncio.get_running_loop()
            signed_order = await loop.run_in_executor(
                None,
                self.client.create_order,
                order
            )
            
            response = await loop.run_in_executor(
                None,
                self.client.post_order,
                signed_order,
                OrderType.GTC if order_type.upper() == "GTC" else OrderType.GTC
            )
            
            order_id = response.get('orderID') or response.get('order_id') or 'unknown'
            logger.info("order_placed_successfully", order_id=order_id)
            return {"success": True, "order_id": order_id, "response": response}
            
        except Exception as e:
            logger.error(
                "order_placement_failed",
                error=str(e),
                error_type=type(e).__name__,
                market=market_id,
                token=token_id,
                side=side
            )
            return {"success": False, "error": str(e)}
    
    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100
    ) -> List[Dict]:
        """
        Get list of markets.
        
        Args:
            active: Only return active markets
            limit: Max markets to return
        
        Returns:
            List of market dicts
        """
        if not self.client:
            return []
        
        try:
            await self._throttle()
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                self.client.get_markets
            )
            
            logger.debug("get_markets_raw_response_type", type=type(response).__name__)
            
            # Handle dict response (paginated API)
            if isinstance(response, dict):
                logger.debug("get_markets_response_is_dict", keys=list(response.keys()))
                # Try common pagination keys
                markets_list = response.get('data') or response.get('markets') or []
            elif isinstance(response, list):
                markets_list = response
            else:
                logger.warning("get_markets_unexpected_type", type=type(response).__name__)
                return []
            
            # Validate we have a list
            if not isinstance(markets_list, list):
                logger.warning("get_markets_list_not_list_after_extraction", type=type(markets_list).__name__)
                return []
            
            logger.debug("get_markets_parsed", total_count=len(markets_list))
            
            if active:
                markets_list = [m for m in markets_list if isinstance(m, dict) and not m.get("closed", False)]
            
            return markets_list[:limit]
            
        except Exception as e:
            logger.error("markets_fetch_failed", error=str(e), exc_info=True)
            return []
    
    async def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """
        Get orderbook for a token.
        
        Args:
            token_id: Token ID
        
        Returns:
            Orderbook dict with 'bids' and 'asks', or None
        """
        if not self.client:
            return None
        
        try:
            await self._throttle()
            loop = asyncio.get_running_loop()
            orderbook = await loop.run_in_executor(
                None,
                self.client.get_order_book,
                token_id
            )
            return orderbook
            
        except Exception as e:
            logger.warning("orderbook_fetch_failed", token_id=token_id, error=str(e))
            return None
    
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order.
        
        Args:
            order_id: Order ID to cancel
        
        Returns:
            True if cancelled successfully
        """
        if self.paper_trading:
            logger.info("paper_order_cancelled", order_id=order_id)
            return True
        
        if not self.authenticated or not self.client:
            logger.error("cannot_cancel_order", reason="not_authenticated")
            return False
        
        try:
            await self._throttle()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                self.client.cancel,
                order_id
            )
            logger.info("order_cancelled_successfully", order_id=order_id)
            return True
            
        except Exception as e:
            logger.error("order_cancellation_failed", order_id=order_id, error=str(e))
            return False
    
    async def health_check(self) -> bool:
        """
        Perform health check.
        
        Returns:
            True if API is healthy
        """
        try:
            markets = await self.get_markets(limit=1)
            return True
        except Exception as e:
            logger.error("health_check_failed", error=str(e))
            return False
    
    def get_metrics(self) -> Dict:
        """
        Get client metrics.
        
        Returns:
            Metrics dictionary
        """
        return {
            "authenticated": self.authenticated,
            "can_trade": self.can_trade,
            "paper_trading": self.paper_trading,
            "has_client": bool(self.client),
            "has_address": bool(self.address),
            "address": self.address
        }
    
    async def close(self):
        """Close client and cleanup resources."""
        logger.info("client_closed", address=self.address)
