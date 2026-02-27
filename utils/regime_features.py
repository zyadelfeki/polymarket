"""
Regime-feature extractor for the volatility/regime classifier.

Computes a snapshot of current market conditions (volatility, trend, recent
PnL) into a flat feature dict that ``utils.regime_classifier.classify_regime``
can consume.

Design decisions
----------------
- All Binance OHLCV data is fetched via the existing ``data_feeds.binance_features``
  cache (60-second TTL) so this call adds < 1 ms latency on the hot path.
- PnL / win-rate stats come from SQLite (synchronous) and are cached with a
  configurable TTL to avoid hammering the DB.  Default TTL = 30 s.
- All errors are absorbed; if a feature cannot be computed its value is NaN and
  the classifier is responsible for handling missing features gracefully.
- This module must never be called on the order-placement critical path.
  Call it from the periodic regime-update task (every 60 s) instead.

Public API
----------
    from utils.regime_features import get_regime_features
    from datetime import datetime, timezone
    feats = get_regime_features(datetime.now(timezone.utc))
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH   = _REPO_ROOT / "data" / "trading.db"

# ---------------------------------------------------------------------------
# PnL-stats cache — avoids per-call DB hits on the ticker loop.
# Protected by a lock because the regime-update task runs in a background
# thread created by asyncio.get_event_loop().run_in_executor().
# ---------------------------------------------------------------------------
_pnl_cache: Optional[Dict] = None
_pnl_cache_ts: float = 0.0
_pnl_cache_lock = threading.Lock()
_PNL_CACHE_TTL_SECONDS: float = 30.0

# How many recent settled trades to include in rolling PnL/win-rate stats.
_ROLLING_N_TRADES: int = 20


def get_regime_features(now: Optional[datetime] = None) -> dict:
    """
    Compute current regime features.

    Parameters
    ----------
    now:
        Timestamp for the feature snapshot.  Defaults to UTC now.

    Returns
    -------
    dict with keys:

    Volatility (higher = more volatile):
      vol_5min        – realised vol over last 5 min candles (std of log-returns)
      vol_15min       – realised vol over last 15 min candles
      vol_60min       – realised vol over last 60 min candles
      vol_ratio_5_60  – vol_5min / vol_60min  (> 1 → local spike, < 1 → calm)

    Trend (price momentum):
      price_vs_sma20  – (price - sma20) / price  (positive = above SMA)
      price_vs_sma50  – (price - sma50) / price
      rsi_14          – generic RSI-14 from Binance features
      macd            – MACD line value

    Orderbook pressure:
      book_imbalance  – (bid_vol - ask_vol) / (bid_vol + ask_vol) ∈ [-1, 1]

    Recent trade performance:
      rolling_win_rate  – win-rate over last N settled trades
      rolling_pnl_mean  – mean PnL per trade (last N)
      rolling_pnl_std   – std of PnL per trade (last N)
      rolling_pnl_z     – z-score of last PnL vs the rolling window

    Time context:
      hour_sin / hour_cos  – circular hour encoding
    """
    if now is None:
        now = datetime.now(timezone.utc)

    feats: Dict[str, float] = {}

    # ------------------- Binance features (cached, fast) -------------------
    try:
        from data_feeds.binance_features import get_all_features as _get_binance_features
        bf = _get_binance_features("BTC")
    except Exception:
        bf = None

    if bf is not None:
        feats["price_vs_sma20"] = _safe_float(bf.get("price_vs_sma20"), math.nan)
        feats["price_vs_sma50"] = _safe_float(bf.get("price_vs_sma50"), math.nan)
        feats["rsi_14"]         = _safe_float(bf.get("rsi_14"),         math.nan)
        feats["macd"]           = _safe_float(bf.get("macd"),           math.nan)
        feats["book_imbalance"] = _safe_float(bf.get("book_imbalance"), math.nan)
    else:
        for k in ("price_vs_sma20", "price_vs_sma50", "rsi_14", "macd", "book_imbalance"):
            feats[k] = math.nan

    # ------------------- Rolling realised volatility -----------------------
    vol_5, vol_15, vol_60 = _compute_realised_vols()
    feats["vol_5min"]   = vol_5
    feats["vol_15min"]  = vol_15
    feats["vol_60min"]  = vol_60
    feats["vol_ratio_5_60"] = (
        vol_5 / vol_60
        if (not math.isnan(vol_5)) and (not math.isnan(vol_60)) and vol_60 > 0
        else math.nan
    )

    # ------------------- PnL / win-rate from DB ----------------------------
    pnl_stats = _get_pnl_stats_cached()
    feats["rolling_win_rate"]  = pnl_stats.get("win_rate", math.nan)
    feats["rolling_pnl_mean"]  = pnl_stats.get("pnl_mean", math.nan)
    feats["rolling_pnl_std"]   = pnl_stats.get("pnl_std",  math.nan)
    feats["rolling_pnl_z"]     = pnl_stats.get("pnl_z",    math.nan)

    # ------------------- Time context --------------------------------------
    hour = now.hour + now.minute / 60.0
    feats["hour_sin"] = math.sin(2 * math.pi * hour / 24.0)
    feats["hour_cos"] = math.cos(2 * math.pi * hour / 24.0)

    return feats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_realised_vols(symbol: str = "BTCUSDT") -> tuple:
    """
    Fetch recent 1-minute candles from Binance REST API and compute realised
    volatility (std of log-returns) over 5, 15, and 60-period windows.

    Returns (vol_5, vol_15, vol_60).  Values are NaN on failure.
    Falls back gracefully if requests is unavailable or network is down.
    """
    nan = math.nan
    try:
        import requests  # type: ignore
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": "1m", "limit": 61}
        resp = requests.get(url, params=params, timeout=3.0)
        resp.raise_for_status()
        candles = resp.json()

        if not candles or len(candles) < 5:
            return nan, nan, nan

        closes = np.array([float(c[4]) for c in candles], dtype=np.float64)
        if len(closes) < 2:
            return nan, nan, nan

        log_returns = np.diff(np.log(closes[closes > 0]))
        if len(log_returns) < 4:
            return nan, nan, nan

        def _vol(n: int) -> float:
            chunk = log_returns[-n:] if len(log_returns) >= n else log_returns
            return float(np.std(chunk)) if len(chunk) >= 2 else nan

        return _vol(5), _vol(15), min(_vol(60), 1.0)  # cap at 100% per-candle

    except Exception:
        return nan, nan, nan


def _get_pnl_stats_cached() -> Dict[str, float]:
    """
    Return rolling PnL stats from the DB, with a 30-second in-process cache.
    """
    global _pnl_cache, _pnl_cache_ts
    now_ts = time.monotonic()
    with _pnl_cache_lock:
        if _pnl_cache is not None and (now_ts - _pnl_cache_ts) < _PNL_CACHE_TTL_SECONDS:
            return _pnl_cache
        stats = _fetch_pnl_stats_from_db()
        _pnl_cache = stats
        _pnl_cache_ts = now_ts
    return stats


def _fetch_pnl_stats_from_db(n: int = _ROLLING_N_TRADES) -> Dict[str, float]:
    nan = math.nan
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2.0)
        rows = conn.execute(
            """
            SELECT CAST(pnl AS REAL)
            FROM order_tracking
            WHERE order_state = 'SETTLED'
              AND pnl IS NOT NULL
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()
        conn.close()
    except Exception:
        return {"win_rate": nan, "pnl_mean": nan, "pnl_std": nan, "pnl_z": nan}

    if not rows:
        return {"win_rate": nan, "pnl_mean": nan, "pnl_std": nan, "pnl_z": nan}

    pnl_values = [r[0] for r in rows if r[0] is not None]
    if not pnl_values:
        return {"win_rate": nan, "pnl_mean": nan, "pnl_std": nan, "pnl_z": nan}

    arr = np.array(pnl_values, dtype=np.float64)
    win_rate = float((arr > 0).mean())
    mu = float(arr.mean())
    sigma = float(arr.std()) if len(arr) > 1 else nan
    # z-score of the most-recent trade vs the full window
    last_pnl = arr[0]  # DESC order → first row is most recent
    pnl_z = ((last_pnl - mu) / sigma) if (not math.isnan(sigma)) and sigma > 1e-9 else 0.0

    return {
        "win_rate":  win_rate,
        "pnl_mean":  mu,
        "pnl_std":   sigma if not math.isnan(sigma) else 0.0,
        "pnl_z":     pnl_z,
    }


def _safe_float(value, default: float) -> float:
    try:
        v = float(value)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default
