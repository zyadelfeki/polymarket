"""
Binance technical feature computation for Charlie signal enrichment.

All computation is pure Python — no pandas, no numpy.
Uses the Binance REST API for candle history and depth data.

Caching:
  - Candle-based features: 60-second TTL per symbol
  - Order book imbalance: 5-second TTL per symbol (order book changes fast)

Returns None on any error — never raises.  Callers must handle None gracefully.

Features computed:
  rsi_14            RSI over 14 periods (Wilder smoothing)
  macd              EMA(12) - EMA(26) of close prices
  price_vs_sma20    (close[-1] - SMA20) / SMA20
  price_vs_sma50    (close[-1] - SMA50) / SMA50
  volatility_20d    std-dev of log returns over last 20 candles
  book_imbalance    (bid_qty_top5 - ask_qty_top5) / (bid_qty_top5 + ask_qty_top5)
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Dict, Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level caches
# ---------------------------------------------------------------------------
_CANDLE_CACHE: Dict[str, tuple] = {}   # symbol -> (timestamp, features_dict)
_BOOK_CACHE:   Dict[str, tuple] = {}   # symbol -> (timestamp, imbalance_float)
_CANDLE_TTL         = 60.0   # seconds — successful fetch TTL
_CANDLE_FAILURE_TTL = 30.0   # seconds — failed fetch TTL (prevent hammering)
_BOOK_TTL           = 5.0    # seconds

_BINANCE_BASE = "https://api.binance.com"


# ---------------------------------------------------------------------------
# Pure-Python math helpers
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average (EMA) using standard smoothing factor k=2/(N+1)."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    ema_val = values[0]
    for v in values:
        ema_val = v * k + ema_val * (1.0 - k)
        result.append(ema_val)
    return result


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """RSI using Wilder's smoothed average (not simple EMA)."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))

    # Initial averages (simple mean of first `period` values)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Binance API helpers (synchronous httpx for simplicity; called in background)
# ---------------------------------------------------------------------------

def _fetch_candles(symbol: str, interval: str = "15m", limit: int = 60) -> Optional[list]:
    """Fetch recent klines from Binance REST.  Returns list of close prices or None."""
    try:
        url = f"{_BINANCE_BASE}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        raw = resp.json()
        # Each kline: [open_time, open, high, low, CLOSE, volume, ...]
        closes = [float(k[4]) for k in raw]
        return closes
    except Exception as exc:
        logger.warning("binance_candle_fetch_failed", symbol=symbol, error=str(exc))
        _CANDLE_CACHE[symbol] = (time.monotonic(), None)   # Cache the failure — prevents retry spam
        return None


def _fetch_depth_imbalance(symbol: str, limit: int = 5) -> tuple:
    """Fetch order book top-N and compute imbalance plus raw book levels.

    Returns
    -------
    tuple: (imbalance: float | None, bids: list, asks: list)
        imbalance is in [-1, 1]; bids/asks are [(price_str, qty_str), ...].
        On failure, returns (None, [], []).
    """
    try:
        url = f"{_BINANCE_BASE}/api/v3/depth"
        params = {"symbol": symbol, "limit": limit}
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
        book = resp.json()
        raw_bids = book.get("bids", [])[:limit]
        raw_asks = book.get("asks", [])[:limit]
        bid_qty = sum(float(b[1]) for b in raw_bids)
        ask_qty = sum(float(a[1]) for a in raw_asks)
        total = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total if total != 0.0 else 0.0
        # Convert to (float, float) tuples for OFI calculator compatibility
        bids = [(float(b[0]), float(b[1])) for b in raw_bids]
        asks = [(float(a[0]), float(a[1])) for a in raw_asks]
        return imbalance, bids, asks
    except Exception as exc:
        logger.warning("binance_depth_fetch_failed", symbol=symbol, error=str(exc))
        return None, [], []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def symbol_for_asset(asset: str) -> str:
    """Map a casual asset name to a Binance USDT symbol."""
    mapping = {
        "BTC": "BTCUSDT",
        "BITCOIN": "BTCUSDT",
        "ETH": "ETHUSDT",
        "ETHEREUM": "ETHUSDT",
        "SOL": "SOLUSDT",
        "SOLANA": "SOLUSDT",
        "MATIC": "MATICUSDT",
        "POLYGON": "MATICUSDT",
        "DOGE": "DOGEUSDT",
        "XRP": "XRPUSDT",
    }
    return mapping.get(asset.upper(), "BTCUSDT")


def get_candle_features(asset: str) -> Optional[Dict[str, float]]:
    """
    Compute candle-based technical features for the given asset.
    Results are module-level cached for 60 seconds.

    Returns a dict with keys:
      rsi_14, macd, price_vs_sma20, price_vs_sma50, volatility_20d

    Returns None if data cannot be fetched or computed.
    """
    symbol = symbol_for_asset(asset)
    now = time.monotonic()

    # Check cache — uses a shorter TTL for cached failures to suppress retry spam
    cached = _CANDLE_CACHE.get(symbol)
    if cached is not None:
        ts, features = cached
        ttl = _CANDLE_TTL if features is not None else _CANDLE_FAILURE_TTL
        if now - ts < ttl:
            return features   # Returns None for cached failures — correct

    closes = _fetch_candles(symbol, interval="15m", limit=60)
    if closes is None or len(closes) < 27:
        # Need at least 27 candles for EMA(26) seed
        logger.warning("binance_features_insufficient_data",
                       symbol=symbol, candles=len(closes) if closes else 0)
        _CANDLE_CACHE[symbol] = (now, None)   # Cache the failure — prevents retry spam
        return None

    try:
        # RSI-14
        rsi_val = _rsi(closes, 14)

        # MACD = EMA(12) - EMA(26)
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_val = ema12[-1] - ema26[-1] if ema12 and ema26 else None

        # price_vs_sma20 / sma50
        sma20 = _sma(closes, 20)
        sma50 = _sma(closes, 50)
        last_close = closes[-1]
        price_vs_sma20 = ((last_close - sma20) / sma20) if sma20 and sma20 != 0 else None
        price_vs_sma50 = ((last_close - sma50) / sma50) if sma50 and sma50 != 0 else None

        # Volatility: std of log returns over last 20 candles
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(max(1, len(closes) - 20), len(closes))
            if closes[i - 1] > 0
        ]
        if len(log_returns) >= 2:
            vol_raw = _std(log_returns)
            vol_20d = vol_raw * math.sqrt(96)  # scale 15m → daily (96 candles/day)
        else:
            vol_20d = None

        # --- Regime guard keys ---
        # Current price (last close from the candle series)
        price = last_close

        # 1-hour price change: 4 × 15m candles back
        price_1h_ago = closes[-5] if len(closes) >= 5 else closes[0]
        price_change_1h = ((last_close - price_1h_ago) / price_1h_ago * 100.0
                           if price_1h_ago != 0.0 else 0.0)

        # ATR% over last 14 candles as a volatility proxy.
        # Requires high/low candle data; we approximate using close-to-close range.
        # True ATR needs raw klines, but close std ÷ mean gives a comparable signal.
        atr_window = closes[-15:] if len(closes) >= 15 else closes
        if len(atr_window) >= 2:
            ranges = [abs(atr_window[i] - atr_window[i - 1]) for i in range(1, len(atr_window))]
            atr_raw = sum(ranges) / len(ranges)
            mean_price = sum(atr_window) / len(atr_window)
            atr_pct = (atr_raw / mean_price * 100.0) if mean_price != 0.0 else 1.0
        else:
            atr_pct = 1.0

        features = {
            k: v for k, v in {
                "rsi_14": rsi_val,
                "macd": macd_val,
                "price_vs_sma20": price_vs_sma20,
                "price_vs_sma50": price_vs_sma50,
                "volatility_20d": vol_20d,
                "price": price,
                "price_change_1h": price_change_1h,
                "atr_pct": atr_pct,
            }.items()
            if v is not None
        }

        if not features:
            return None

        _CANDLE_CACHE[symbol] = (now, features)
        logger.info(
            "binance_features_computed",
            symbol=symbol,
            rsi_14=round(features.get("rsi_14", 0), 2),
            macd=round(features.get("macd", 0), 6),
            price=round(features.get("price", 0), 2),
            price_change_1h=round(features.get("price_change_1h", 0), 4),
            atr_pct=round(features.get("atr_pct", 0), 4),
            price_vs_sma20=round(features.get("price_vs_sma20", 0), 6),
            price_vs_sma50=round(features.get("price_vs_sma50", 0), 6),
            volatility_20d=round(features.get("volatility_20d", 0), 6),
        )
        return features

    except Exception as exc:
        logger.warning("binance_features_compute_failed", symbol=symbol, error=str(exc))
        _CANDLE_CACHE[symbol] = (now, None)   # Cache the failure — prevents retry spam
        return None


def get_book_imbalance(asset: str) -> Optional[float]:
    """
    Compute top-5 order book imbalance for the given asset.
    Cached for 5 seconds.

    Returns float in [-1.0, 1.0]:
      +1.0 = pure buy pressure
      -1.0 = pure sell pressure
      None = data unavailable
    """
    symbol = symbol_for_asset(asset)
    now = time.monotonic()

    cached = _BOOK_CACHE.get(symbol)
    if cached is not None:
        ts, imbalance = cached
        if now - ts < _BOOK_TTL:
            return imbalance

    imbalance, bids, asks = _fetch_depth_imbalance(symbol, limit=5)
    if imbalance is not None:
        _BOOK_CACHE[symbol] = (now, imbalance)
        logger.debug("binance_book_imbalance",
                     symbol=symbol, imbalance=round(imbalance, 4))
    return imbalance


def get_all_features(asset: str) -> Optional[Dict]:
    """
    Fetch and merge candle features + book imbalance + raw book levels.

    Returns combined dict or None if candle features unavailable.

    Extra keys (when book fetch succeeds):
      book_imbalance : float  — (bid_qty - ask_qty) / total, in [-1, 1]
      ofi_bids       : list   — [(price, size), ...] top-5 bid levels
      ofi_asks       : list   — [(price, size), ...] top-5 ask levels
    These are consumed by OFICalculator in charlie_booster.py.
    """
    features = get_candle_features(asset)
    if features is None:
        return None
    symbol = symbol_for_asset(asset)
    imbalance, bids, asks = _fetch_depth_imbalance(symbol, limit=5)
    if imbalance is not None:
        features = {**features, "book_imbalance": round(imbalance, 6)}
    if bids:
        features["ofi_bids"] = bids
    if asks:
        features["ofi_asks"] = asks
    return features


async def get_binance_features(
    binance_symbol: str,
    interval: str = "15m",
) -> Optional[Dict[str, float]]:
    """
    Async entry point for computing Binance technical features.

    Parameters
    ----------
    binance_symbol : str
        Binance trading pair e.g. "BTCUSDT", "ETHUSDT", "SOLUSDT".
        Also accepts short names like "BTC", "ETH" (mapped internally).
    interval : str
        Candle interval.  Currently only "15m" is cached; other intervals
        bypass the cache and fetch fresh data.

    Returns
    -------
    Dict with keys: rsi_14, macd, price_vs_sma20, price_vs_sma50,
    volatility_20d (+ book_imbalance if available).
    Returns None on any error — never raises.
    """
    # Strip USDT/USD/PERP suffix so symbol_for_asset() recognises the base asset.
    # "BTCUSDT" -> "BTC",  "ETHUSDT" -> "ETH",  "BTC" -> "BTC" (already stripped)
    base = binance_symbol.upper()
    for suffix in ("USDT", "USD", "PERP"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_all_features, base)
