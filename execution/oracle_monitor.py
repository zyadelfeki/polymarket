"""
UMA Optimistic Oracle Window Monitor.

After a Polymarket 15-minute BTC market closes, UMA's Optimistic Oracle has
a 2-hour liveness window.  During this window, the outcome is KNOWN (we can
verify from the Binance candle close) but Polymarket shares haven't resolved
to $0 or $1 yet.  Impatient sellers may leave resting asks at $0.96-$0.99
on guaranteed winners — creating a risk-free arb.

Currently LOG-ONLY — monitors and reports, does not place orders.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import structlog

logger = structlog.get_logger(__name__)

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
UMA_LIVENESS_HOURS = 2  # hours after close where arb window is open


def get_btc_candle_close(
    interval: str, open_time_utc: datetime
) -> Optional[dict]:
    """
    Fetch the exact BTC/USDT candle for a given interval and open time.

    Returns dict with open_time, close, high, low, open, is_closed.
    Returns None on any error (network, parse, etc.).
    """
    import requests

    open_ts_ms = int(open_time_utc.timestamp() * 1000)
    params = {
        "symbol": "BTCUSDT",
        "interval": interval,
        "startTime": open_ts_ms,
        "limit": 1,
    }
    try:
        r = requests.get(BINANCE_KLINE_URL, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        k = data[0]
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return {
            "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            "open": Decimal(k[1]),
            "high": Decimal(k[2]),
            "low": Decimal(k[3]),
            "close": Decimal(k[4]),
            "is_closed": now_ms > k[6],  # k[6] is candle close time ms
        }
    except Exception as exc:
        logger.warning(
            "btc_candle_fetch_failed",
            error=str(exc),
            interval=interval,
        )
        return None


def check_oracle_window(market: dict) -> Optional[dict]:
    """
    For a recently-closed BTC direction market, verify the outcome
    from Binance and check if the Polymarket price hasn't resolved yet.

    Returns opportunity dict if arb window is open, None otherwise.

    Parameters
    ----------
    market : dict
        Market dict with end_date_iso / end_date, condition_id, question.
    """
    close_ts = (
        market.get("end_date_iso")
        or market.get("end_date")
        or market.get("close_time")
    )
    if not close_ts:
        return None

    try:
        close_dt = datetime.fromisoformat(str(close_ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    now = datetime.now(timezone.utc)
    minutes_since_close = (now - close_dt).total_seconds() / 60

    # Only process markets that closed within the last 2 hours
    if not (0 < minutes_since_close < UMA_LIVENESS_HOURS * 60):
        return None

    # Get the candle that this market resolved on
    candle = get_btc_candle_close("15m", close_dt - timedelta(minutes=15))
    if not candle or not candle["is_closed"]:
        return None

    question = market.get("question", "") or ""

    logger.info(
        "oracle_window_detected",
        market_id=market.get("condition_id") or market.get("market_id"),
        minutes_since_close=round(minutes_since_close, 1),
        candle_close=float(candle["close"]),
        candle_open=float(candle["open"]),
        candle_direction="UP" if candle["close"] > candle["open"] else "DOWN",
        market_question=question[:80],
    )

    return {
        "market_id": market.get("condition_id") or market.get("market_id"),
        "candle_close": candle["close"],
        "candle_open": candle["open"],
        "candle_direction": "UP" if candle["close"] > candle["open"] else "DOWN",
        "minutes_since_close": minutes_since_close,
        "market_question": question[:80],
    }
