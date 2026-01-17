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
import structlog

try:
    from web3 import Web3
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams
    from py_clob_client.order_builder.constants import BUY, SELL
    from eth_account import Account
    POLYMARKET_AVAILABLE = True
except ImportError:
    POLYMARKET_AVAILABLE = False

logger = structlog.get_logger(__name__)


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
        
        # Load proxy address from environment
        self.proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        
        # Initialize Web3 for balance checks
        self.w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        
        # State tracking
        self.client: Optional[Any] = None
        self.address: Optional[str] = None
        self.can_trade = False
        self.authenticated = False
        
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
            loop = asyncio.get_event_loop()
            
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
                    {"token_id": "yes", "price": 0.50},
                    {"token_id": "no", "price": 0.50}
                ],
                "mock": True
            }
        
        try:
            logger.debug("fetching_market_via_direct_lookup", market_id=market_id)
            
            loop = asyncio.get_event_loop()
            market = await loop.run_in_executor(
                None,
                lambda: self.client.get_market(market_id)
            )
            
            logger.debug("direct_market_lookup_response_type", type=type(market).__name__)
            
            # Validate response
            if not isinstance(market, dict):
                logger.warning("market_response_not_dict", type=type(market).__name__)
                return {"tokens": [], "error": "invalid_response", "market_id": market_id}
            
            # Ensure tokens field exists
            if 'tokens' not in market:
                logger.warning("market_missing_tokens_field", market_id=market_id)
                market['tokens'] = []
            
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
    
    async def place_order(
        self,
        market_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float
    ) -> Optional[str]:
        """
        Place an order.
        
        Args:
            market_id: Market ID
            token_id: Token ID
            side: 'BUY' or 'SELL'
            price: Limit price (0.01-0.99)
            size: Order size
        
        Returns:
            Order ID or None
        """
        # Paper trading simulation
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
            return order_id
        
        # Check authentication for live trading
        if not self.authenticated or not self.client:
            logger.error("cannot_place_order", reason="not_authenticated")
            return None
        
        # Validate inputs
        if not (0.01 <= price <= 0.99):
            logger.error("invalid_price", price=price, valid_range="0.01-0.99")
            return None
        
        if size <= 0:
            logger.error("invalid_size", size=size)
            return None
        
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
            order_side = BUY if side.upper() == "BUY" else SELL
            order = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side
            )
            
            # Sign and post
            loop = asyncio.get_event_loop()
            signed_order = await loop.run_in_executor(
                None,
                self.client.create_order,
                order
            )
            
            response = await loop.run_in_executor(
                None,
                self.client.post_order,
                signed_order,
                OrderType.GTC
            )
            
            order_id = response.get('orderID', 'unknown')
            logger.info("order_placed_successfully", order_id=order_id)
            return order_id
            
        except Exception as e:
            logger.error(
                "order_placement_failed",
                error=str(e),
                error_type=type(e).__name__,
                market=market_id,
                token=token_id,
                side=side
            )
            return None
    
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
            loop = asyncio.get_event_loop()
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
            loop = asyncio.get_event_loop()
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
            loop = asyncio.get_event_loop()
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
