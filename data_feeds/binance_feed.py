"""
Real-time Binance @trade stream feed.

Subscribes to individual trade events (NOT @aggTrade which buffers 100ms).
Maintains a live price cache and fires registered callbacks on each fill.

This supplements the existing BinanceWebSocketV2 (which uses @miniTicker)
with per-trade granularity needed for last-second sniping.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
import structlog

logger = structlog.get_logger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws"


class BinanceTradeFeed:
    """
    Subscribes to Binance @trade streams for per-fill price updates.

    Usage::

        feed = BinanceTradeFeed(["BTC", "ETH", "SOL"])
        feed.register_callback(my_handler)
        asyncio.create_task(feed.run())

        # Later:
        price = feed.get_price("BTC")  # Decimal or None
    """

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = [s.upper() for s in symbols]
        self._prices: dict[str, Decimal] = {}
        self._timestamps: dict[str, datetime] = {}
        self._callbacks: list[Callable] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def prices(self) -> dict[str, Decimal]:
        return dict(self._prices)

    def register_callback(
        self, fn: Callable[[str, Decimal, datetime], None]
    ) -> None:
        """Register fn(symbol, price, timestamp) called on each trade."""
        self._callbacks.append(fn)

    def get_price(self, symbol: str) -> Optional[Decimal]:
        """Get latest price for symbol (e.g. 'BTC'). None if no data."""
        return self._prices.get(symbol.upper())

    def get_timestamp(self, symbol: str) -> Optional[datetime]:
        """Get timestamp of the latest trade for symbol."""
        return self._timestamps.get(symbol.upper())

    async def run(self) -> None:
        """Connect and stream indefinitely with auto-reconnect."""
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error(
                "binance_trade_feed_unavailable",
                reason="websockets package not installed",
            )
            return

        streams = "/".join(f"{s.lower()}usdt@trade" for s in self.symbols)
        url = f"{BINANCE_WS}/{streams}"
        self._running = True

        while self._running:
            try:
                async with websockets.connect(
                    url, ping_interval=20, close_timeout=5
                ) as ws:
                    logger.info(
                        "binance_trade_feed_connected",
                        streams=streams,
                        symbols=self.symbols,
                    )
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            # Handle combined stream wrapper
                            if "data" in data:
                                data = data["data"]
                            if data.get("e") != "trade":
                                continue
                            symbol = data["s"].replace("USDT", "")
                            price = Decimal(data["p"])
                            ts = datetime.fromtimestamp(
                                data["T"] / 1000, tz=timezone.utc
                            )
                            self._prices[symbol] = price
                            self._timestamps[symbol] = ts

                            for cb in self._callbacks:
                                try:
                                    cb(symbol, price, ts)
                                except Exception as exc:
                                    logger.warning(
                                        "binance_trade_callback_error",
                                        error=str(exc),
                                    )
                        except (json.JSONDecodeError, KeyError):
                            continue

            except asyncio.CancelledError:
                logger.info("binance_trade_feed_cancelled")
                break
            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "binance_trade_feed_disconnected",
                    error=str(exc),
                    reconnecting_in=5,
                )
                await asyncio.sleep(5)

        logger.info("binance_trade_feed_stopped")

    def stop(self) -> None:
        """Signal the feed to stop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
