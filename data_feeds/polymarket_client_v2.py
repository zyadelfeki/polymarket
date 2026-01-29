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
import uuid
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Optional, Dict, List, Any, Callable, Awaitable, TypeVar
from enum import Enum
try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

_DISABLE_SDK_ENV = os.getenv("POLYMARKET_DISABLE_SDK", "").lower() in {"1", "true", "yes"}

getcontext().prec = 18

try:
    if _DISABLE_SDK_ENV:
        raise ImportError("Polymarket SDK disabled via env")
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
            kwargs = inject_correlation(kwargs)
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

        def critical(self, event: str, **kwargs):
            self._log(logging.CRITICAL, event, **kwargs)

    logger = _FallbackLogger(__name__)

from services.error_codes import ErrorCode
from services.correlation_context import CorrelationContext, inject_correlation
from services.validators import BoundaryValidator
from services.network_health import NetworkHealthMonitor

T = TypeVar("T")


class OrderSide(Enum):
    """Order side enum"""
    BUY = "BUY"
    SELL = "SELL"


class TokenBucket:
    """Async token bucket rate limiter."""

    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.1)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        start = time.monotonic()

        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated_at = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True

            if timeout is not None and (time.monotonic() - start) >= timeout:
                return False

            await asyncio.sleep(0.01)


class RequestMetrics:
    """Track request success/failure and latency."""

    def __init__(self):
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.total_latency_ms = 0.0

    def record_request(self, success: bool, latency_ms: float) -> None:
        self.total_requests += 1
        if success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1
        self.total_latency_ms += float(latency_ms)

    def get_metrics(self) -> Dict:
        avg_latency = 0.0
        if self.total_requests > 0:
            avg_latency = self.total_latency_ms / self.total_requests
        success_rate = 0.0
        if self.total_requests > 0:
            success_rate = self.successful_requests / self.total_requests
        return {
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": success_rate,
            "avg_latency_ms": avg_latency,
        }


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
        paper_trading: bool = True,
        retry_backoff_base: float = 1.0
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
        self.retry_backoff_base = max(retry_backoff_base, 0.0)
        
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
        self.rate_limiter = TokenBucket(rate=self.rate_limit, capacity=1.0)
        self.metrics = RequestMetrics()
        self.network_monitor = NetworkHealthMonitor()

        # Auth retry + key rotation
        self.auth_retry_count = 0
        self.max_auth_retries = 3
        self.api_key_rotation_enabled = os.getenv('API_KEY_ROTATION', 'false').lower() == 'true'
        self.backup_api_keys: List[Dict[str, str]] = self._load_backup_api_keys()
        self._active_backup_key_index = 0
        self.emergency_shutdown_reason: Optional[str] = None
        self._auth_failure_handler: Optional[Callable[[str], Awaitable[None]]] = None
        
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
                logger.warning("api_key_rotated", index=self._active_backup_key_index)
                return True
        except Exception as e:
            logger.error("api_key_rotation_failed", error=str(e))
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

    @staticmethod
    def _classify_error(status_code: Optional[int], error: Exception) -> str:
        if status_code in (401, 403):
            return "auth"
        if status_code is not None and 500 <= status_code <= 599:
            return "server"
        if isinstance(error, (asyncio.TimeoutError, OSError)):
            return "network"
        message = str(error).lower()
        if "timeout" in message or "connection" in message or "network" in message:
            return "network"
        return "unknown"

    async def _execute_with_retries(
        self,
        operation: str,
        func: Callable[[], Awaitable[T]]
    ) -> Optional[T]:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                start = time.monotonic()
                result = await func()
                latency_ms = (time.monotonic() - start) * 1000
                self.metrics.record_request(True, latency_ms)
                self.network_monitor.record_success()
                self.auth_retry_count = 0
                return result
            except Exception as exc:
                last_error = exc
                status_code = self._extract_http_status(exc)
                category = self._classify_error(status_code, exc)
                self.metrics.record_request(False, 0.0)
                self.network_monitor.record_failure(str(exc))

                if category == "auth" and status_code is not None:
                    should_retry = await self._handle_auth_error(status_code)
                    if not should_retry:
                        break
                elif category in {"server", "network"}:
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_backoff_base * (2 ** attempt))
                        continue
                break

        if last_error is not None:
            logger.error("request_failed", operation=operation, error=str(last_error))
        return None

    async def _handle_auth_error(self, error_code: int) -> bool:
        """Handle 401/403 with exponential backoff and optional key rotation."""
        self.auth_retry_count += 1

        if error_code == 401:
            logger.error("api_key_invalid_attempting_rotation")
            if self.api_key_rotation_enabled and self.backup_api_keys:
                rotated = self._rotate_api_key()
                if rotated:
                    return True

        if self.auth_retry_count >= self.max_auth_retries:
            await self._emergency_shutdown("AUTH_FAILURE_CRITICAL")
            return False

        await asyncio.sleep(2 ** self.auth_retry_count)
        return True

    async def _emergency_shutdown(self, reason: str) -> None:
        """Disable trading on critical auth failures."""
        self.can_trade = False
        self.authenticated = False
        self.emergency_shutdown_reason = reason
        logger.critical("emergency_shutdown", reason=reason)
        if self._auth_failure_handler:
            try:
                await self._auth_failure_handler(reason)
            except Exception as exc:
                logger.error("auth_failure_handler_failed", error=str(exc))

    def set_auth_failure_handler(self, handler: Callable[[str], Awaitable[None]]) -> None:
        """Register async handler for critical auth failures."""
        self._auth_failure_handler = handler
    
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
        if self.rate_limiter:
            await self.rate_limiter.acquire(tokens=1.0)
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
        
        async def _fetch_balance() -> Decimal:
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
                bridged=str(bridged_decimal),
                native=str(native_decimal),
                total=str(total_decimal)
            )

            return total_decimal

        result = await self._execute_with_retries("get_usdc_balance", _fetch_balance)
        if result is None:
            logger.error("web3_balance_fetch_failed", proxy=self.proxy_address)
        return result if result is not None else Decimal('0')
    
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
                    {"token_id": "yes", "outcome": "YES", "price": Decimal("0.50")},
                    {"token_id": "no", "outcome": "NO", "price": Decimal("0.50")}
                ],
                "yes_price": Decimal("0.50"),
                "no_price": Decimal("0.50"),
                "yes_token_id": "yes",
                "no_token_id": "no",
                "mock": True
            }
        
        async def _fetch_market() -> Optional[Dict]:
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

            if not isinstance(market, dict):
                logger.warning("market_response_not_dict", type=type(market).__name__)
                market = None

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

        try:
            market = await self._execute_with_retries("get_market", _fetch_market)
            if market is None:
                return {"tokens": [], "error": "market_not_found", "market_id": market_id}
            return market
        except AttributeError as e:
            logger.error(
                "get_market_method_not_available",
                error=str(e),
                market_id=market_id,
                message="get_market() not found in ClobClient"
            )
            return {"tokens": [], "error": "method_not_available", "market_id": market_id}

    async def _fetch_market_via_gamma(self, condition_id: str) -> Optional[Dict]:
        """Fallback market lookup using Gamma API by condition id."""
        import httpx

        async def _fetch_gamma() -> Optional[Dict]:
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

        result = await self._execute_with_retries("get_market_gamma", _fetch_gamma)
        if result is None:
            logger.error("gamma_market_lookup_failed", condition_id=condition_id)
        return result

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

    def _extract_token_price(self, token: Dict) -> Optional[Decimal]:
        price = token.get('price')
        if price is None:
            price = token.get('last_price')
        try:
            if price is None:
                return None
            return Decimal(str(price)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        except Exception:
            return None

    async def _get_best_ask(self, token_id: Optional[str]) -> Optional[Decimal]:
        if not token_id:
            return None
        await self._throttle()
        orderbook = await self.get_orderbook(token_id)
        if not orderbook:
            return None
        asks = orderbook.get("asks", [])
        if asks:
            try:
                price = asks[0].get("price")
                if price is None:
                    return None
                return Decimal(str(price)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            except Exception:
                return None
        return None
    
    async def place_order(
        self,
        token_id: str,
        side: Any,
        price: Any,
        size: Any,
        order_type: str = "GTC",
        market_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ):
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

        from services.execution_service_v2 import OrderResult, OrderStatus

        correlation_id = correlation_id or CorrelationContext.get()

        if isinstance(price, float) or isinstance(size, float):
            logger.error(
                "invalid_decimal_input",
                **inject_correlation({
                    "error": "float_not_allowed",
                    "error_code": ErrorCode.INVALID_ORDER.value
                })
            )
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="float_not_allowed",
                error_code=ErrorCode.INVALID_ORDER.value,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )

        try:
            price_dec = BoundaryValidator.validate_price(price)
            size_dec = BoundaryValidator.validate_quantity(size)
        except ValueError as exc:
            error_message = str(exc)
            error_code = ErrorCode.INVALID_ORDER.value
            if "price" in error_message.lower():
                error_code = ErrorCode.INVALID_PRICE.value
            elif "quantity" in error_message.lower():
                error_code = ErrorCode.INVALID_QUANTITY.value
            logger.error(
                "invalid_order_input",
                **inject_correlation({
                    "error": error_message,
                    "error_code": error_code
                })
            )
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error=error_message,
                error_code=error_code,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )

        if self.paper_trading:
            order_id = f"paper_{uuid.uuid4().hex}"
            logger.info(
                "paper_order_placed",
                order_id=order_id,
                market=market_id,
                token=token_id,
                side=side,
                price=str(price_dec),
                size=str(size_dec),
                correlation_id=correlation_id
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                status=OrderStatus.SUBMITTED,
                filled_quantity=Decimal("0"),
                filled_price=Decimal("0"),
                fees=Decimal("0"),
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )
        
        # Check authentication for live trading
        if not self.authenticated or not self.client:
            logger.error(
                "cannot_place_order",
                **inject_correlation({
                    "reason": "not_authenticated",
                    "error_code": ErrorCode.NOT_AUTHENTICATED.value
                })
            )
            return OrderResult(
                success=False,
                status=OrderStatus.REJECTED,
                error="not_authenticated",
                error_code=ErrorCode.NOT_AUTHENTICATED.value,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )
        
        async def _submit_order() -> Dict:
            logger.info(
                "placing_live_order",
                market=market_id,
                token=token_id,
                side=side,
                price=str(price_dec),
                size=str(size_dec),
                correlation_id=correlation_id
            )

            if idempotency_key:
                try:
                    if hasattr(self.client, "set_header"):
                        self.client.set_header("Idempotency-Key", idempotency_key)
                    elif hasattr(self.client, "session") and hasattr(self.client.session, "headers"):
                        self.client.session.headers["Idempotency-Key"] = idempotency_key
                except Exception:
                    logger.debug("idempotency_header_set_failed", correlation_id=correlation_id)

            side_str = side.value if isinstance(side, OrderSide) else str(side)
            order_side = SELL if side_str.upper() == "SELL" else BUY
            order = OrderArgs(
                token_id=token_id,
                price=float(price_dec),
                size=float(size_dec),
                side=order_side
            )

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
            return response

        response = await self._execute_with_retries("place_order", _submit_order)
        if response is None:
            return OrderResult(
                success=False,
                status=OrderStatus.FAILED,
                error="auth_retry_exhausted",
                error_code=ErrorCode.NOT_AUTHENTICATED.value,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key
            )

        order_id = response.get('orderID') or response.get('order_id') or 'unknown'
        logger.info("order_placed_successfully", order_id=order_id)
        return OrderResult(
            success=True,
            order_id=order_id,
            status=OrderStatus.SUBMITTED,
            filled_quantity=Decimal("0"),
            filled_price=Decimal("0"),
            fees=Decimal("0"),
            correlation_id=correlation_id,
            idempotency_key=idempotency_key
        )
    
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

        async def _fetch_markets() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self.client.get_markets
            )

        response = await self._execute_with_retries("get_markets", _fetch_markets)
        if response is None:
            return []

        logger.debug("get_markets_raw_response_type", type=type(response).__name__)

        if isinstance(response, dict):
            logger.debug("get_markets_response_is_dict", keys=list(response.keys()))
            markets_list = response.get('data') or response.get('markets') or []
        elif isinstance(response, list):
            markets_list = response
        else:
            logger.warning("get_markets_unexpected_type", type=type(response).__name__)
            return []

        if not isinstance(markets_list, list):
            logger.warning("get_markets_list_not_list_after_extraction", type=type(markets_list).__name__)
            return []

        logger.debug("get_markets_parsed", total_count=len(markets_list))

        if active:
            markets_list = [m for m in markets_list if isinstance(m, dict) and not m.get("closed", False)]

        return markets_list[:limit]

    async def get_active_markets(self, limit: int = 100) -> List[Dict]:
        """Compatibility helper for strategy modules."""
        return await self.get_markets(active=True, limit=limit)

    async def get_positions(self) -> List[Dict]:
        """Get open positions if available."""
        if self.paper_trading:
            return []
        if not self.authenticated or not self.client:
            logger.error("cannot_get_positions", reason="not_authenticated")
            return []

        async def _fetch_positions() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            if hasattr(self.client, "get_positions"):
                return await loop.run_in_executor(None, self.client.get_positions)
            if hasattr(self.client, "list_positions"):
                return await loop.run_in_executor(None, self.client.list_positions)
            logger.warning("get_positions_not_available")
            return []

        positions = await self._execute_with_retries("get_positions", _fetch_positions)
        return positions if isinstance(positions, list) else []

    async def get_open_positions(self) -> List[Dict]:
        """Alias for get_positions with safe defaults."""
        positions = await self.get_positions()
        if isinstance(positions, list):
            return positions
        return []

    async def get_account_balance(self) -> Decimal:
        """Return total USDC balance as Decimal."""
        if self.paper_trading:
            return Decimal("0")
        try:
            return await self.get_usdc_balance()
        except Exception as e:
            logger.error("balance_fetch_failed", error=str(e))
            return Decimal("0")

    def _parse_orderbook_side(self, entries: List[Dict], pick_max: bool) -> tuple:
        best_price = None
        total_size = Decimal("0")

        for entry in entries or []:
            try:
                price_val = entry.get("price") if isinstance(entry, dict) else None
                size_val = entry.get("size") if isinstance(entry, dict) else None
                if price_val is None:
                    continue
                price_dec = Decimal(str(price_val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                size_dec = Decimal(str(size_val)) if size_val is not None else Decimal("0")
                total_size += size_dec

                if best_price is None:
                    best_price = price_dec
                else:
                    if pick_max and price_dec > best_price:
                        best_price = price_dec
                    if not pick_max and price_dec < best_price:
                        best_price = price_dec
            except Exception:
                continue

        if best_price is None:
            best_price = Decimal("0")
        return best_price, total_size

    async def get_market_orderbook_summary(self, market_id: str) -> Optional[Dict]:
        """Return best bid/ask summary for a market (YES token)."""
        market = await self.get_market(market_id)
        if not market or market.get("error"):
            return None
        yes_token_id = market.get("yes_token_id")
        if not yes_token_id:
            return None
        orderbook = await self.get_orderbook(yes_token_id)
        if not orderbook:
            yes_price = market.get("yes_price")
            if yes_price is None:
                return None
            yes_price = Decimal(str(yes_price)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            return {
                "market_id": market_id,
                "bid": yes_price,
                "ask": yes_price,
                "bid_volume": Decimal("0"),
                "ask_volume": Decimal("0"),
            }
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        bid_price, bid_size = self._parse_orderbook_side(bids, pick_max=True)
        ask_price, ask_size = self._parse_orderbook_side(asks, pick_max=False)

        return {
            "market_id": market_id,
            "bid": bid_price,
            "ask": ask_price,
            "bid_volume": bid_size,
            "ask_volume": ask_size,
        }
    
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

        async def _fetch_orderbook() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None,
                self.client.get_order_book,
                token_id
            )

        orderbook = await self._execute_with_retries("get_orderbook", _fetch_orderbook)
        if orderbook is None:
            logger.warning("orderbook_fetch_failed", token_id=token_id)
        return orderbook
    
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

        async def _cancel() -> bool:
            await self._throttle()
            loop = asyncio.get_running_loop()
            if hasattr(self.client, "cancel"):
                await loop.run_in_executor(None, self.client.cancel, order_id)
            elif hasattr(self.client, "cancel_order"):
                await loop.run_in_executor(None, self.client.cancel_order, order_id)
            else:
                logger.warning("cancel_order_not_available")
                return False
            return True

        result = await self._execute_with_retries("cancel_order", _cancel)
        if result:
            logger.info("order_cancelled_successfully", order_id=order_id)
        return bool(result)

    async def get_order_status(self, order_id: str) -> Optional[Dict]:
        """Get order status for an order ID."""
        if self.paper_trading:
            return {"order_id": order_id, "status": "filled", "fills": []}

        if not self.authenticated or not self.client:
            logger.error("cannot_get_order_status", reason="not_authenticated")
            return None

        async def _fetch_status() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            if hasattr(self.client, "get_order"):
                return await loop.run_in_executor(None, self.client.get_order, order_id)
            if hasattr(self.client, "get_order_status"):
                return await loop.run_in_executor(None, self.client.get_order_status, order_id)
            logger.warning("get_order_status_not_available")
            return None

        result = await self._execute_with_retries("get_order_status", _fetch_status)
        if result is None:
            logger.error("order_status_fetch_failed", order_id=order_id)
        return result
    
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
        metrics = {
            "authenticated": self.authenticated,
            "can_trade": self.can_trade,
            "paper_trading": self.paper_trading,
            "has_client": bool(self.client),
            "has_address": bool(self.address),
            "address": self.address
        }
        metrics.update(self.metrics.get_metrics())
        return metrics
    
    async def close(self):
        """Close client and cleanup resources."""
        logger.info("client_closed", address=self.address)
