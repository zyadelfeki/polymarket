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
from datetime import datetime, timezone, timedelta
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

        def exception(self, event: str, **kwargs):
            self._log(logging.ERROR, event, exc_info=True, **kwargs)

        def critical(self, event: str, **kwargs):
            self._log(logging.CRITICAL, event, **kwargs)

    logger = _FallbackLogger(__name__)

from services.error_codes import ErrorCode
from services.correlation_context import CorrelationContext, inject_correlation
from services.validators import BoundaryValidator
from services.network_health import NetworkHealthMonitor
from execution.order_types import OrderResult
from utils.decimal_helpers import to_decimal, safe_decimal, quantize_price, quantize_quantity

T = TypeVar("T")


class OrderSide(Enum):
    """Order side enum"""
    BUY = "BUY"
    SELL = "SELL"


class OrderResultDict(dict):
    """Runtime dict with attribute access for OrderResult compatibility."""

    def __getattr__(self, item: str):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __bool__(self) -> bool:
        return bool(self.get("success", False))


def _make_order_result(
    *,
    success: bool,
    order_id: Optional[str],
    error: Optional[str],
    filled_size: Optional[Decimal],
    avg_price: Optional[Decimal],
    timestamp: float,
) -> OrderResult:
    return OrderResultDict(
        success=success,
        order_id=order_id,
        error=error,
        filled_size=filled_size,
        avg_price=avg_price,
        timestamp=timestamp,
    )


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
        self.total_latency_ms += latency_ms

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
        self.clob_base_url = self.host
        self.chain_id = 137  # Polygon Mainnet
        
        self.private_key = private_key
        self.paper_trading = paper_trading
        self.max_retries = max_retries
        self.timeout = timeout
        self.rate_limit = max(rate_limit, 0.1)
        self.retry_backoff_base = max(retry_backoff_base, 0.0)
        
        # Load proxy address from environment
        self.proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
        self.wallet_address = self.proxy_address
        
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

        # Instance-level market lookup cache.  Keyed by market_id; each entry is
        # (result_dict, timestamp).  Prevents the settlement loop from fetching the
        # same market N times (once per open order) on every maintenance cycle.
        self._get_market_cache: dict = {}
        self._get_market_cache_ttl: float = 60.0  # seconds
        
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
                    if should_retry and attempt < self.max_retries:
                        continue
                    break
                elif category in {"server", "network"}:
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_backoff_base * (2 ** attempt))
                        continue
                    break
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

    def _derive_api_credentials(self) -> tuple[Any, str]:
        """Derive API credentials across supported py-clob-client SDK variants."""
        derive_api_key = getattr(self.client, "create_or_derive_api_key", None)
        if callable(derive_api_key):
            return derive_api_key(), "create_or_derive_api_key"

        derive_api_creds = getattr(self.client, "create_or_derive_api_creds", None)
        if callable(derive_api_creds):
            return derive_api_creds(), "create_or_derive_api_creds"

        raise AttributeError("No credential derivation method available on ClobClient")
    
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
            logger.warning("no_private_key_readonly_mode")
            # Create read-only client
            try:
                self.client = ClobClient(self.host)
                logger.info("readonly_client_initialized")
            except Exception as e:
                logger.error("readonly_client_failed", error=str(e))
            return
        
        try:
            # Step 1: Create client with private key
            self.client = ClobClient(
                host=self.host,
                key=self.private_key,
                chain_id=self.chain_id
            )
            
            # Step 2: Derive wallet address
            account = Account.from_key(self.private_key)
            self.address = account.address
            if not self.wallet_address:
                self.wallet_address = self.address
            
            # Step 3: For LIVE mode, derive API credentials NOW
            if not self.paper_trading:
                try:
                    creds, credential_method = self._derive_api_credentials()
                    if credential_method == "create_or_derive_api_key":
                        self.client = ClobClient(
                            host=self.host,
                            key=self.private_key,
                            chain_id=self.chain_id,
                            creds=creds
                        )
                    elif hasattr(self.client, "set_api_creds"):
                        self.client.set_api_creds(creds)
                    else:
                        self.client = ClobClient(
                            host=self.host,
                            key=self.private_key,
                            chain_id=self.chain_id,
                            creds=creds
                        )

                    self.authenticated = True
                    logger.info(
                        "live_auth_success",
                        address=self.address,
                        credential_method=credential_method,
                        proxy_address=self.proxy_address,
                    )
                except Exception as e:
                    logger.error(
                        "credential_derivation_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        address=self.address
                    )
                    self.authenticated = False
            else:
                # Paper trading: no auth needed
                self.authenticated = True
                logger.info("paper_mode_initialized", address=self.address)
            
            # Mark as ready to trade
            self.can_trade = self.paper_trading or self.authenticated
            
        except Exception as e:
            logger.error(
                "client_init_failed",
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
            market_id: Market condition_id (0x...) or integer id
        
        Returns:
            Market dict or safe default with tokens field
        """
        import time as _time
        # Serve from instance-level TTL cache to avoid repeated Gamma API calls
        # for the same market within one settlement scan (one call per open order
        # would otherwise fire N sequential throttled requests for N orders).
        _now = _time.monotonic()
        _cached = self._get_market_cache.get(market_id)
        if _cached is not None:
            _result, _ts = _cached
            if _now - _ts < self._get_market_cache_ttl:
                return _result

        if not self.client:
            logger.error("cannot_fetch_market", reason="client_uninitialized", market_id=market_id)
            return {"tokens": [], "error": "client_uninitialized", "market_id": market_id}
        
        async def _fetch_market() -> Optional[Dict]:
            await self._throttle()
            logger.debug("fetching_market_via_direct_lookup", market_id=market_id)

            # Integer IDs (e.g. "1403228") are Gamma/REST API IDs — CLOB only
            # understands hex condition_ids.  Skip the CLOB call entirely and go
            # straight to Gamma to avoid a guaranteed exception/None that would
            # prevent the Gamma fallback from running.
            market = None
            if str(market_id).lstrip("-").isdigit():
                logger.debug("integer_market_id_using_gamma_direct", market_id=market_id)
            elif hasattr(self.client, "get_market"):
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

            # Gamma API responses for integer-ID markets use outcomePrices/outcomes
            # instead of a tokens array.  If the Gamma response has resolution fields
            # return it as-is — the settlement loop only needs those, not token data.
            if market and 'tokens' not in market and (
                'outcomePrices' in market
                or 'active' in market
                or 'resolutionTime' in market
            ):
                logger.info(
                    "market_found_via_gamma_resolution",
                    market_id=market_id,
                    active=market.get('active'),
                    closed=market.get('closed'),
                    question=market.get('question', 'N/A')[:50],
                )
                return market
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
                result = {"tokens": [], "error": "market_not_found", "market_id": market_id}
            else:
                result = market
        except AttributeError as e:
            logger.error(
                "get_market_method_not_available",
                error=str(e),
                market_id=market_id,
                message="get_market() not found in ClobClient"
            )
            result = {"tokens": [], "error": "method_not_available", "market_id": market_id}
        # Store in instance cache (cache even errors to avoid retry storms).
        import time as _time
        self._get_market_cache[market_id] = (result, _time.monotonic())
        return result

    async def _fetch_market_via_gamma(self, condition_id: str) -> Optional[Dict]:
        """Fallback market lookup using Gamma API by condition_id or integer id."""
        import httpx

        async def _fetch_gamma() -> Optional[Dict]:
            await self._throttle()
            base_url = "https://gamma-api.polymarket.com/markets"
            candidates = []
            # Integer market IDs (e.g. "1403228" from the Gamma REST API) can be
            # fetched directly via /markets/{id}.  Hex condition IDs use the
            # query-param path.  Try integer path first to avoid wasted calls.
            if str(condition_id).lstrip("-").isdigit():
                candidates = [
                    f"{base_url}/{condition_id}",
                    f"{base_url}?id={condition_id}",
                ]
            else:
                candidates = [
                    f"{base_url}?condition_ids={condition_id}",
                    f"{base_url}?condition_id={condition_id}",
                    f"{base_url}?conditionId={condition_id}",
                ]
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for url in candidates:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if isinstance(data, dict) and (data.get('id') or data.get('conditionId')):
                        return data
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
        side: str,
        size: Decimal,
        price: Decimal,
        order_type: str = "GTC",
        market_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> OrderResult:
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

        correlation_id = correlation_id or CorrelationContext.get()
        side_str = side.value if hasattr(side, "value") else str(side)
        normalized_side = side_str.strip().upper()
        if normalized_side in {"YES", "NO"}:
            normalized_side = "BUY"

        if normalized_side not in {"BUY", "SELL"}:
            logger.error(
                "invalid_order_input",
                **inject_correlation({
                    "error": f"invalid_side:{side_str}",
                    "error_code": ErrorCode.INVALID_ORDER.value
                })
            )
            return _make_order_result(
                success=False,
                order_id=None,
                error=f"invalid_side:{side_str}",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        try:
            price_dec = quantize_price(BoundaryValidator.validate_price(to_decimal(price)))
            size_dec = quantize_quantity(BoundaryValidator.validate_quantity(to_decimal(size)))
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
            return _make_order_result(
                success=False,
                order_id=None,
                error=error_message,
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        if size_dec <= Decimal("0"):
            return _make_order_result(
                success=False,
                order_id=None,
                error="size_must_be_positive",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        if not (Decimal("0") < price_dec < Decimal("1")):
            return _make_order_result(
                success=False,
                order_id=None,
                error="price_out_of_range",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        if self.paper_trading:
            order_id = f"paper_{uuid.uuid4().hex}"
            logger.info(
                "paper_order_placed",
                order_id=order_id,
                market=market_id,
                token=token_id,
                side=normalized_side,
                price=str(price_dec),
                size=str(size_dec),
                correlation_id=correlation_id
            )
            return _make_order_result(
                success=True,
                order_id=order_id,
                error=None,
                filled_size=Decimal("0"),
                avg_price=price_dec,
                timestamp=time.time(),
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
            return _make_order_result(
                success=False,
                order_id=None,
                error="not_authenticated",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )
        
        async def _submit_order() -> Dict:
            logger.info(
                "placing_live_order",
                market=market_id,
                token=token_id,
                side=normalized_side,
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

            order_side = SELL if normalized_side == "SELL" else BUY
            order = OrderArgs(
                token_id=token_id,
                price=price_dec,
                size=size_dec,
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

        try:
            response = await self._execute_with_retries("place_order", _submit_order)
        except Exception as exc:
            logger.exception("order_exception", error=str(exc))
            return _make_order_result(
                success=False,
                order_id=None,
                error=f"Exception: {str(exc)}",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )
        if response is None:
            return _make_order_result(
                success=False,
                order_id=None,
                error="auth_retry_exhausted",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        if not isinstance(response, dict):
            return _make_order_result(
                success=False,
                order_id=None,
                error=f"invalid_api_response:{type(response).__name__}",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        order_id = response.get("orderID") or response.get("order_id")
        if not order_id:
            return _make_order_result(
                success=False,
                order_id=None,
                error=f"invalid_api_response:{response}",
                filled_size=None,
                avg_price=None,
                timestamp=time.time(),
            )

        filled_size = response.get("filled") or response.get("filled_size")
        avg_price = response.get("avgPrice") or response.get("avg_price") or price_dec
        logger.info("order_placed_successfully", order_id=order_id)
        return _make_order_result(
            success=True,
            order_id=order_id,
            error=None,
            filled_size=safe_decimal(filled_size) if filled_size is not None else Decimal("0"),
            avg_price=safe_decimal(avg_price) if avg_price is not None else price_dec,
            timestamp=time.time(),
        )

    async def get_wallet_balance(self) -> Optional[Dict[str, Any]]:
        """Fetch wallet balance from client or fallback sources."""
        if self.paper_trading:
            balance = os.getenv("PAPER_TRADING_BALANCE") or os.getenv("INITIAL_CAPITAL") or "0"
            return {"balance": balance}

        if not self.client:
            logger.error("wallet_balance_unavailable", reason="client_uninitialized")
            return None

        if hasattr(self.client, "get_wallet_balance"):
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self.client.get_wallet_balance)
            except Exception as exc:
                logger.error("wallet_balance_fetch_failed", error=str(exc))
                return None

        logger.error("wallet_balance_unavailable", reason="unsupported_client")
        return None

    async def get_live_balance(self) -> Optional[Decimal]:
        """Fetch available collateral balance from CLOB balances endpoint."""
        if self.paper_trading:
            balance_raw = os.getenv("PAPER_TRADING_BALANCE") or os.getenv("INITIAL_CAPITAL") or "0"
            try:
                return Decimal(str(balance_raw))
            except Exception:
                return Decimal("0")

        balance_address = self.wallet_address or self.proxy_address or self.address
        if not balance_address:
            logger.error("live_balance_address_missing")
            return None

        try:
            import httpx

            async def _fetch_live_balance() -> Optional[Decimal]:
                await self._throttle()
                url = f"{self.clob_base_url}/balances/{balance_address}"
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(url)
                    if response.status_code != 200:
                        logger.error("balance_fetch_failed", status=response.status_code)
                        return None

                    data = response.json()
                    collateral_raw = data.get("collateral", "0") if isinstance(data, dict) else "0"
                    balance = Decimal(str(collateral_raw)) / Decimal("1000000")
                    logger.info("live_balance_fetched", balance=str(balance), address=balance_address)
                    return balance

            return await self._execute_with_retries("get_live_balance", _fetch_live_balance)
        except Exception as e:
            logger.error("balance_fetch_error", error=str(e))
            return None
    
    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        **filters: Any
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

        gamma_markets = await self._fetch_markets_via_gamma(active=active, limit=limit)
        if gamma_markets:
            logger.info("markets_fetched_gamma", total=len(gamma_markets), active=active)
            return gamma_markets[:limit]

        filters_payload = dict(filters) if filters else {}
        next_cursor = filters_payload.get("next_cursor")
        if len(filters_payload) > 1 or (filters_payload and next_cursor is None):
            logger.warning("unsupported_get_markets_filters", filters=list(filters_payload.keys()))
            next_cursor = None

        async def _fetch_markets() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            def _call():
                if next_cursor is not None:
                    return self.client.get_markets(next_cursor)
                return self.client.get_markets()
            return await loop.run_in_executor(None, _call)

        response = await self._execute_with_retries("get_markets", _fetch_markets)
        markets_list = self._parse_markets_response(response, operation="get_markets")
        if not markets_list:
            return []

        if active:
            markets_list = self._filter_live_markets(markets_list)

        return markets_list[:limit]

    async def _fetch_markets_via_gamma(self, *, active: bool, limit: int) -> List[Dict]:
        """Fetch markets from Gamma API with explicit active/closed/archived filters."""
        try:
            import httpx

            requested_limit = max(limit, 1)
            page_limit = min(max(requested_limit, 100), 500)
            max_pages = 5

            all_markets: List[Dict] = []
            seen_ids = set()

            for page in range(max_pages):
                params = {
                    "limit": str(page_limit),
                    "offset": str(page * page_limit),
                    "active": "true" if active else "false",
                    "closed": "false" if active else "true",
                    "archived": "false" if active else "true",
                }

                async def _call_gamma_page() -> Any:
                    await self._throttle()
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.get("https://gamma-api.polymarket.com/markets", params=params)
                        if response.status_code != 200:
                            logger.warning(
                                "gamma_markets_unexpected_status",
                                status=response.status_code,
                                params=params,
                            )
                            return None
                        return response.json()

                payload = await self._execute_with_retries("gamma_get_markets", _call_gamma_page)
                page_markets = self._parse_markets_response(payload, operation="gamma_get_markets")
                if not page_markets:
                    break

                for market in page_markets:
                    if not isinstance(market, dict):
                        continue
                    market_id = (
                        market.get("id")
                        or market.get("condition_id")
                        or market.get("conditionId")
                        or market.get("slug")
                    )
                    if market_id in seen_ids:
                        continue
                    seen_ids.add(market_id)
                    all_markets.append(market)

                if len(page_markets) < page_limit:
                    break
                if len(all_markets) >= requested_limit:
                    break

            if not all_markets:
                return []

            if active:
                all_markets = self._filter_live_markets(all_markets)

            return all_markets[:requested_limit]
        except Exception as exc:
            logger.warning("gamma_market_fetch_failed", error=str(exc))
            return []

    def _filter_live_markets(self, markets: List[Dict]) -> List[Dict]:
        filtered: List[Dict] = []
        rejected = 0
        for market in markets:
            if not isinstance(market, dict):
                rejected += 1
                continue

            if market.get("closed") is True:
                rejected += 1
                continue

            if market.get("archived") is True:
                rejected += 1
                continue

            status = str(market.get("status") or "").upper()
            if status in {"CLOSED", "RESOLVED", "SETTLED", "FINALIZED", "EXPIRED"}:
                rejected += 1
                continue

            end_dt = self._extract_market_end_datetime(market)
            if end_dt is not None and end_dt <= datetime.now(timezone.utc):
                rejected += 1
                continue

            filtered.append(market)

        logger.info("live_market_filter_summary", total=len(markets), accepted=len(filtered), rejected=rejected)
        return filtered

    def _normalize_gamma_prices(self, markets: List[Dict]) -> List[Dict]:
        """Backfill yes_price/no_price from Gamma outcomePrices/outcomes arrays."""
        for market in markets:
            if not isinstance(market, dict):
                continue
            if market.get("yes_price") is not None and market.get("no_price") is not None:
                continue

            outcome_prices = market.get("outcomePrices")
            outcomes = market.get("outcomes")

            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = []
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:
                    outcomes = []

            if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
                continue
            if not isinstance(outcomes, list):
                outcomes = []

            yes_idx, no_idx = 0, 1
            for idx, label in enumerate(outcomes):
                normalized = str(label).strip().lower()
                if normalized in {"up", "yes"}:
                    yes_idx = idx
                elif normalized in {"down", "no"}:
                    no_idx = idx

            try:
                yes_price = Decimal(str(outcome_prices[yes_idx])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                no_price = Decimal(str(outcome_prices[no_idx])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            except Exception:
                continue

            market["yes_price"] = yes_price
            market["no_price"] = no_price

        return markets

    def _extract_market_end_datetime(self, market: Dict) -> Optional[datetime]:
        def _from_unix_decimal(value: Any) -> datetime:
            seconds_decimal = safe_decimal(value)
            whole_seconds = int(seconds_decimal)
            micros_decimal = (seconds_decimal - Decimal(whole_seconds)) * Decimal("1000000")
            microseconds = int(micros_decimal)
            return datetime.fromtimestamp(whole_seconds, tz=timezone.utc) + timedelta(microseconds=microseconds)

        end_fields = [
            "end_date_iso",
            "endDateIso",
            "endDateISO",
            "endDate",
            "end_date",
            "endTime",
            "end_time",
            "closeTime",
            "close_time",
            "resolve_time",
            "resolution_time",
            "resolutionTime",
            "expires_at",
            "expiresAt",
        ]

        raw_value = None
        for field in end_fields:
            if field in market and market.get(field) not in (None, ""):
                raw_value = market.get(field)
                break

        if raw_value is None:
            return None

        try:
            if isinstance(raw_value, datetime):
                if raw_value.tzinfo is None:
                    return raw_value.replace(tzinfo=timezone.utc)
                return raw_value.astimezone(timezone.utc)

            if isinstance(raw_value, str):
                text = raw_value.strip()
                if not text:
                    return None
                if "T" in text or "Z" in text or "+" in text:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        return parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                numeric = Decimal(text)
                return _from_unix_decimal(numeric)

            if isinstance(raw_value, (int, float, Decimal)):
                return _from_unix_decimal(raw_value)
        except Exception:
            return None

        return None

    def _parse_markets_response(self, response: Any, operation: str) -> List[Dict]:
        if response is None:
            return []

        logger.debug("markets_raw_response_type", operation=operation, type=type(response).__name__)

        if isinstance(response, dict):
            logger.debug("markets_response_is_dict", operation=operation, keys=list(response.keys()))
            markets_list = response.get("data") or response.get("markets") or []
        elif isinstance(response, list):
            markets_list = response
        else:
            logger.warning("markets_unexpected_type", operation=operation, type=type(response).__name__)
            return []

        if not isinstance(markets_list, list):
            logger.warning("markets_list_not_list_after_extraction", operation=operation, type=type(markets_list).__name__)
            return []

        logger.debug("markets_parsed", operation=operation, total_count=len(markets_list))
        return markets_list

    async def get_crypto_15min_markets(self) -> List[Dict]:
        """
        Fetch 15-minute crypto markets using slug-based discovery.

        15-minute markets follow a predictable naming pattern:
        {asset}-updown-15m-{unix_timestamp}
        """
        try:
            import httpx
            from datetime import datetime, timedelta, timezone as _tz

            markets: List[Dict] = []
            gamma_base = "https://gamma-api.polymarket.com/events/slug"

            assets = ["btc", "eth", "sol", "xrp"]

            # Use timezone-aware UTC now so that .timestamp() on all derived
            # datetimes is correct regardless of the host machine's local timezone.
            now_utc = datetime.now(_tz.utc)
            et_offset = timedelta(hours=-5)
            now_et = now_utc + et_offset

            minutes_et = (now_et.minute // 15) * 15
            current_interval_et = now_et.replace(minute=minutes_et, second=0, microsecond=0)
            current_interval_utc = current_interval_et - et_offset

            intervals_to_check = [
                current_interval_utc - timedelta(minutes=30),
                current_interval_utc - timedelta(minutes=15),
                current_interval_utc,
                current_interval_utc + timedelta(minutes=15),
                current_interval_utc + timedelta(minutes=30),
                current_interval_utc + timedelta(minutes=45),
            ]

            logger.debug(
                "checking_15min_intervals_ET",
                current_utc=now_utc.isoformat(),
                current_et=now_et.isoformat(),
                intervals_et=[
                    (interval + et_offset).strftime("%H:%M ET")
                    for interval in intervals_to_check
                ],
            )

            # Build the deduplicated slug list.  Only offset=0 is used because the
            # ET→UTC interval computation is exact (EST = UTC-5 in Feb; no DST).
            # The former ±1h/±2h offsets produced ~100 extra 404 requests per cycle,
            # causing the asyncio.wait_for(timeout=10s) in _get_active_markets to
            # always fire before the function could return any results.
            slugs_to_check: List[tuple] = []  # (asset, slug)
            seen_slugs: set = set()
            for asset in assets:
                for interval in intervals_to_check:
                    unix_ts = int(interval.timestamp())
                    slug = f"{asset}-updown-15m-{unix_ts}"
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        slugs_to_check.append((asset, slug))

            logger.debug(
                "15min_slug_candidates",
                total=len(slugs_to_check),
                assets=assets,
                intervals=len(intervals_to_check),
            )

            diagnostic_logged = False

            async def _fetch_one(asset: str, slug: str, http_client: "httpx.AsyncClient") -> None:
                """Fetch a single slug event and append valid markets to `markets`."""
                nonlocal diagnostic_logged
                try:
                    url = f"{gamma_base}/{slug}"
                    response = await http_client.get(url)

                    if response.status_code == 200:
                        event = response.json()

                        if not diagnostic_logged and asset == "btc":
                            diagnostic_logged = True
                            logger.info(
                                "DIAGNOSTIC_event_structure",
                                slug=slug,
                                top_level_keys=list(event.keys()),
                                closed=event.get("closed"),
                                has_markets_array=("markets" in event),
                                markets_count=(len(event.get("markets", [])) if "markets" in event else "N/A"),
                                sample_event=str(event)[:500],
                            )

                        if event.get("closed", False):
                            logger.debug("event_closed", slug=slug)
                            return

                        event_markets = event.get("markets", [])

                        if not event_markets:
                            if "conditionId" in event or "condition_id" in event:
                                event_markets = [event]
                            else:
                                logger.debug(
                                    "no_markets_in_event",
                                    slug=slug,
                                    event_keys=list(event.keys())[:10],
                                )
                                return

                        active_markets = [m for m in event_markets if not m.get("closed", False)]

                        if active_markets:
                            normalized_markets: List[Dict] = []
                            for market_item in active_markets:
                                if not isinstance(market_item, dict):
                                    continue
                                normalized_markets.append(
                                    {
                                        **market_item,
                                        "question": market_item.get("question") or event.get("title") or event.get("question"),
                                        "title": market_item.get("title") or event.get("title") or event.get("question"),
                                        "slug": market_item.get("slug") or event.get("slug") or event.get("ticker"),
                                        "startDate": market_item.get("startDate") or event.get("startDate") or event.get("startTime"),
                                        "endDate": market_item.get("endDate") or event.get("endDate") or event.get("closedTime"),
                                        "end_date_iso": market_item.get("end_date_iso") or event.get("endDate"),
                                        "event_id": market_item.get("event_id") or event.get("id"),
                                    }
                                )

                            markets.extend(normalized_markets)
                            logger.info(
                                "15min_market_found",
                                asset=asset.upper(),
                                slug=slug,
                                markets_count=len(normalized_markets),
                                end_date=event.get("endDate") or event.get("end_date"),
                                market_ids=[
                                    (m.get("conditionId") or m.get("condition_id") or m.get("id") or "unknown")[:8]
                                    for m in normalized_markets
                                ],
                            )
                        else:
                            logger.debug("all_markets_closed", slug=slug)
                    elif response.status_code == 404:
                        logger.debug("market_not_found", slug=slug)
                    else:
                        logger.warning("unexpected_status", slug=slug, status=response.status_code)
                except Exception as exc:
                    logger.error("slug_fetch_failed", slug=slug, error=str(exc))

            # Run all slug fetches concurrently.  With 24 slugs at ~0.3 s each this
            # completes in ~1–3 s instead of the former ~60 s serial execution.
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                await asyncio.gather(
                    *[_fetch_one(asset, slug, http_client) for asset, slug in slugs_to_check],
                    return_exceptions=True,
                )

            logger.info(
                "crypto_15min_markets_discovered",
                total_found=len(markets),
                assets_checked=len(assets),
                intervals_checked=len(intervals_to_check),
            )

            return markets
        except Exception as exc:
            logger.error(
                "get_crypto_15min_markets_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return []

    async def _fallback_clob_recent_markets(self) -> List[Dict]:
        """
        Fallback: use CLOB API with pagination to reach recent markets.
        """
        if not self.client:
            return []

        try:
            logger.debug("using_clob_fallback")
            all_markets: List[Dict] = []
            next_cursor = "MA=="
            max_pages = 5

            for page in range(max_pages):
                async def _fetch_page() -> Any:
                    await self._throttle()
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(
                        None,
                        self.client.get_markets,
                        next_cursor,
                    )

                response = await self._execute_with_retries("get_markets_fallback", _fetch_page)
                markets_list = self._parse_markets_response(response, operation="get_markets_fallback")
                if not markets_list:
                    break

                if isinstance(response, dict):
                    next_cursor = response.get("next_cursor") or response.get("nextCursor")

                all_markets.extend(markets_list)
                logger.debug(
                    "clob_page_fetched",
                    page=page + 1,
                    markets=len(markets_list),
                    total=len(all_markets),
                )

                if not next_cursor:
                    break

            crypto_15min_markets: List[Dict] = []
            for market in all_markets:
                if not isinstance(market, dict):
                    continue
                if market.get("closed", False):
                    continue

                question = (market.get("question") or "").lower()

                has_crypto = any(asset in question for asset in [
                    "bitcoin", "btc",
                    "ethereum", "eth",
                    "solana", "sol",
                    "xrp", "ripple",
                ])

                is_15min = any(pattern in question for pattern in [
                    "15 minute",
                    "15-minute",
                    "15min",
                ])

                is_directional = any(term in question for term in [
                    "up or down",
                    "rise or fall",
                    "higher or lower",
                ])

                if has_crypto and is_15min and is_directional:
                    crypto_15min_markets.append(market)

            logger.info(
                "clob_fallback_filtered",
                total_scanned=len(all_markets),
                found=len(crypto_15min_markets),
            )

            return crypto_15min_markets
        except Exception as exc:
            logger.error("clob_fallback_failed", error=str(exc))
            return []

    async def get_active_markets(self, limit: int = 100) -> List[Dict]:
        """Compatibility helper for strategy modules."""
        primary_markets = await self.get_markets(active=True, limit=max(limit, 200))
        event_markets = await self._discover_crypto_event_markets(limit=max(limit, 300))

        slug_discovered: List[Dict] = []
        try:
            slug_discovered.extend(await self.get_crypto_15min_markets())
            slug_discovered.extend(await self._discover_updown_markets_by_timeframe("1h"))
            slug_discovered.extend(await self._discover_updown_markets_by_timeframe("4h"))
            slug_discovered.extend(await self._discover_updown_markets_by_timeframe("daily"))
        except Exception as exc:
            logger.warning("slug_discovery_failed", error=str(exc))

        merged: List[Dict] = []
        seen_ids = set()
        for market in [*slug_discovered, *event_markets, *primary_markets]:
            if not isinstance(market, dict):
                continue
            market_id = (
                market.get("id")
                or market.get("condition_id")
                or market.get("conditionId")
                or market.get("slug")
            )
            if market_id in seen_ids:
                continue
            seen_ids.add(market_id)
            merged.append(market)

        if not merged:
            logger.warning("active_markets_discovery_empty")
            return []

        filtered = self._filter_live_markets(merged)
        logger.info(
            "active_markets_discovery_summary",
            primary=len(primary_markets),
            event_discovered=len(event_markets),
            slug_discovered=len(slug_discovered),
            merged=len(merged),
            filtered=len(filtered),
        )
        filtered = self._normalize_gamma_prices(filtered)
        return filtered[:limit]

    async def get_events(
        self,
        *,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
        tag: Optional[str] = None,
    ) -> List[Dict]:
        """Fetch Gamma events with optional tag filter."""
        try:
            import httpx

            params = {
                "limit": str(max(limit, 1)),
                "offset": str(max(offset, 0)),
                "active": "true" if active else "false",
            }
            if tag:
                params["tag"] = tag

            async def _call_events() -> Any:
                await self._throttle()
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get("https://gamma-api.polymarket.com/events", params=params)
                    if response.status_code != 200:
                        logger.warning("gamma_events_unexpected_status", status=response.status_code, params=params)
                        return None
                    return response.json()

            payload = await self._execute_with_retries("gamma_get_events", _call_events)
            if isinstance(payload, list):
                return [event for event in payload if isinstance(event, dict)]
            if isinstance(payload, dict):
                events_list = payload.get("data") or payload.get("events") or []
                if isinstance(events_list, list):
                    return [event for event in events_list if isinstance(event, dict)]
            return []
        except Exception as exc:
            logger.warning("gamma_events_fetch_failed", error=str(exc))
            return []

    async def get_event_by_slug(self, slug: str) -> Optional[Dict]:
        """Fetch a single Gamma event by slug."""
        if not slug:
            return None
        try:
            import httpx

            async def _call_event() -> Any:
                await self._throttle()
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(f"https://gamma-api.polymarket.com/events/slug/{slug}")
                    if response.status_code == 404:
                        return None
                    if response.status_code != 200:
                        logger.warning("gamma_event_unexpected_status", status=response.status_code, slug=slug)
                        return None
                    return response.json()

            payload = await self._execute_with_retries("gamma_get_event_by_slug", _call_event)
            if isinstance(payload, dict):
                return payload
            return None
        except Exception as exc:
            logger.warning("gamma_event_fetch_failed", slug=slug, error=str(exc))
            return None

    async def _discover_crypto_event_markets(self, limit: int) -> List[Dict]:
        """Discover short-horizon crypto event markets from Gamma events inventory."""
        page_size = min(max(limit, 100), 500)
        max_pages = 3

        discovered: List[Dict] = []
        seen_market_ids = set()

        for page in range(max_pages):
            events = await self.get_events(
                active=True,
                tag="crypto",
                limit=page_size,
                offset=page * page_size,
            )
            if not events:
                break

            for event in events:
                event_text = " ".join(
                    [
                        str(event.get("title") or ""),
                        str(event.get("slug") or ""),
                        str(event.get("ticker") or ""),
                        str(event.get("description") or ""),
                    ]
                ).lower()

                if not any(token in event_text for token in ("btc", "bitcoin", "eth", "ethereum", "sol", "xrp")):
                    continue

                event_markets = event.get("markets") if isinstance(event.get("markets"), list) else []
                for market in event_markets:
                    if not isinstance(market, dict):
                        continue
                    normalized = {
                        **market,
                        "question": market.get("question") or event.get("title") or event.get("question"),
                        "title": market.get("title") or event.get("title") or event.get("question"),
                        "slug": market.get("slug") or event.get("slug") or event.get("ticker"),
                        "startDate": market.get("startDate") or event.get("startDate") or event.get("startTime"),
                        "endDate": market.get("endDate") or event.get("endDate") or event.get("closedTime"),
                        "end_date_iso": market.get("end_date_iso") or event.get("endDate"),
                        "event_id": event.get("id"),
                        "event_slug": event.get("slug"),
                    }

                    market_id = (
                        normalized.get("id")
                        or normalized.get("conditionId")
                        or normalized.get("condition_id")
                        or normalized.get("slug")
                    )
                    if market_id in seen_market_ids:
                        continue
                    seen_market_ids.add(market_id)
                    discovered.append(normalized)

            if len(events) < page_size:
                break

        logger.info("crypto_event_markets_discovered", count=len(discovered))
        return discovered[:limit]

    async def _discover_updown_markets_by_timeframe(self, timeframe_slug: str) -> List[Dict]:
        """Discover crypto up/down markets by known slug patterns for 1h/4h/daily."""
        try:
            import httpx
            from datetime import timedelta

            assets = ["btc", "eth", "sol", "xrp"]
            gamma_base = "https://gamma-api.polymarket.com/events/slug"
            now_utc = datetime.now(timezone.utc)
            discovered: List[Dict] = []

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                ts_offsets = [0, 3600, 7200, -3600, -7200]
                seen_slugs = set()

                if timeframe_slug in {"1h", "4h"}:
                    step_seconds = 3600 if timeframe_slug == "1h" else 4 * 3600
                    offsets = range(-3, 5)
                    for asset in assets:
                        for offset in offsets:
                            base_ts = int((now_utc + timedelta(seconds=offset * step_seconds)).timestamp())
                            for ts_offset in ts_offsets:
                                ts = base_ts + ts_offset
                                slug = f"{asset}-updown-{timeframe_slug}-{ts}"
                                if slug in seen_slugs:
                                    continue
                                seen_slugs.add(slug)
                                await self._try_append_slug_event(client, gamma_base, slug, discovered)
                elif timeframe_slug == "daily":
                    offsets = range(-1, 4)
                    for asset in assets:
                        for offset in offsets:
                            date_str = (now_utc + timedelta(days=offset)).strftime("%Y-%m-%d")
                            slug = f"{asset}-updown-{date_str}"
                            await self._try_append_slug_event(client, gamma_base, slug, discovered)

            logger.info("slug_timeframe_discovered", timeframe=timeframe_slug, count=len(discovered))
            return discovered
        except Exception as exc:
            logger.warning("slug_timeframe_discovery_failed", timeframe=timeframe_slug, error=str(exc))
            return []

    async def _try_append_slug_event(
        self,
        client: Any,
        gamma_base: str,
        slug: str,
        discovered: List[Dict],
    ) -> None:
        try:
            await self._throttle()
            response = await client.get(f"{gamma_base}/{slug}")
            if response.status_code != 200:
                return

            event = response.json()
            if not isinstance(event, dict):
                return
            if event.get("closed", False):
                return

            event_markets = event.get("markets") if isinstance(event.get("markets"), list) else []
            if event_markets:
                for market_item in event_markets:
                    if not isinstance(market_item, dict) or market_item.get("closed", False):
                        continue
                    discovered.append(
                        {
                            **market_item,
                            "question": market_item.get("question") or event.get("title") or event.get("question"),
                            "title": market_item.get("title") or event.get("title") or event.get("question"),
                            "slug": market_item.get("slug") or event.get("slug") or event.get("ticker"),
                            "startDate": market_item.get("startDate") or event.get("startDate") or event.get("startTime"),
                            "endDate": market_item.get("endDate") or event.get("endDate") or event.get("closedTime"),
                            "end_date_iso": market_item.get("end_date_iso") or event.get("endDate"),
                            "event_id": market_item.get("event_id") or event.get("id"),
                        }
                    )
            elif event.get("conditionId") or event.get("condition_id"):
                discovered.append(event)
        except Exception:
            return

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

    async def get_open_orders(self) -> List[Dict]:
        """Return currently open exchange orders with a stable dict shape."""
        if not self.client:
            return []

        async def _fetch_open_orders() -> Any:
            await self._throttle()
            loop = asyncio.get_running_loop()
            if hasattr(self.client, "get_open_orders"):
                return await loop.run_in_executor(None, self.client.get_open_orders)
            if hasattr(self.client, "get_orders"):
                return await loop.run_in_executor(None, self.client.get_orders)
            logger.warning("get_open_orders_not_available")
            return []

        raw_orders = await self._execute_with_retries("get_open_orders", _fetch_open_orders)
        if not isinstance(raw_orders, list):
            return []

        normalized_orders: List[Dict] = []
        for order in raw_orders:
            if isinstance(order, dict):
                data = dict(order)
                get_value = data.get
            else:
                data = order
                get_value = lambda key, default=None: getattr(data, key, default)

            normalized_orders.append(
                {
                    "order_id": str(
                        get_value("order_id")
                        or get_value("id")
                        or get_value("orderID")
                        or ""
                    ),
                    "market_id": str(
                        get_value("market_id")
                        or get_value("market")
                        or get_value("condition_id")
                        or ""
                    ),
                    "token_id": str(
                        get_value("token_id")
                        or get_value("asset_id")
                        or get_value("assetId")
                        or ""
                    ),
                    "outcome": str(get_value("outcome") or ""),
                    "side": str(get_value("side") or "BUY"),
                    "size": str(
                        get_value("size")
                        or get_value("original_size")
                        or get_value("amount")
                        or "0"
                    ),
                    "price": str(get_value("price") or "0"),
                    "status": str(get_value("status") or get_value("state") or "SUBMITTED"),
                    "opened_at": get_value("opened_at") or get_value("created_at") or get_value("timestamp"),
                }
            )
        return normalized_orders

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
                price_val = None
                size_val = None
                if isinstance(entry, dict):
                    price_val = entry.get("price")
                    size_val = entry.get("size")
                else:
                    price_val = getattr(entry, "price", None)
                    size_val = getattr(entry, "size", None)
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

    def _normalize_orderbook(self, orderbook: Any) -> Optional[Dict]:
        if orderbook is None:
            return None

        if isinstance(orderbook, dict):
            bids = orderbook.get("bids") or []
            asks = orderbook.get("asks") or []
            return {
                "bids": bids,
                "asks": asks,
                "market": orderbook.get("market"),
                "asset_id": orderbook.get("asset_id") or orderbook.get("assetId") or orderbook.get("token_id"),
                "timestamp": orderbook.get("timestamp"),
                "raw": orderbook,
            }

        bids_obj = getattr(orderbook, "bids", None)
        asks_obj = getattr(orderbook, "asks", None)
        if bids_obj is None and asks_obj is None:
            return None

        def _normalize_side(side_entries: Any) -> List[Dict]:
            normalized: List[Dict] = []
            if not side_entries:
                return normalized
            for level in side_entries:
                if isinstance(level, dict):
                    price_val = level.get("price")
                    size_val = level.get("size")
                else:
                    price_val = getattr(level, "price", None)
                    size_val = getattr(level, "size", None)

                if price_val is None:
                    continue
                normalized.append(
                    {
                        "price": str(price_val),
                        "size": str(size_val) if size_val is not None else "0",
                    }
                )
            return normalized

        return {
            "bids": _normalize_side(bids_obj),
            "asks": _normalize_side(asks_obj),
            "market": getattr(orderbook, "market", None),
            "asset_id": getattr(orderbook, "asset_id", None),
            "timestamp": getattr(orderbook, "timestamp", None),
            "raw": orderbook,
        }

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

        orderbook_raw = await self._execute_with_retries("get_orderbook", _fetch_orderbook)
        if orderbook_raw is None:
            logger.warning("orderbook_fetch_failed", token_id=token_id)

        normalized = self._normalize_orderbook(orderbook_raw)
        if not normalized:
            logger.warning(
                "orderbook_unparseable",
                token_id=token_id,
                orderbook_type=type(orderbook_raw).__name__,
            )
            return None

        logger.info(
            "orderbook_normalized",
            token_id=token_id,
            orderbook_type=type(orderbook_raw).__name__,
            bids_count=len(normalized.get("bids") or []),
            asks_count=len(normalized.get("asks") or []),
        )
        return normalized

    async def get_market_orderbook(self, token_id: str) -> Optional[Dict]:
        """Compatibility alias: always fetches live orderbook data from CLOB."""
        return await self.get_orderbook(token_id)
    
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
