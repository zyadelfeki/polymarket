"""Fetch live BTC indicators from Binance for Charlie extra_features."""
import asyncio, statistics
from typing import Dict
import aiohttp

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

def _ema(prices, period):
    k = 2 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
    return e

async def fetch_btc_features(timeframe: str = "15m", limit: int = 60) -> Dict:
    # Fetch enough candles for SMA-50 + volatility (need 50+)
    params = {"symbol": "BTCUSDT", "interval": timeframe, "limit": limit}
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_KLINES, params=params,
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()

    closes    = [float(c[4]) for c in data]
    highs     = [float(c[2]) for c in data]
    lows      = [float(c[3]) for c in data]
    now       = closes[-1]

    # RSI-14
    gains  = [max(0.0, closes[i]-closes[i-1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i-1]-closes[i]) for i in range(1, len(closes))]
    avg_g  = sum(gains[-14:])  / 14
    avg_l  = sum(losses[-14:]) / 14
    rsi    = 100 - (100 / (1 + avg_g / avg_l)) if avg_l else 50.0

    # MACD (12 EMA - 26 EMA)
    macd = _ema(closes[-26:], 12) - _ema(closes[-26:], 26)

    # Price vs SMA-20 and SMA-50
    sma20          = sum(closes[-20:]) / 20
    sma50          = sum(closes[-50:]) / 50
    price_vs_sma20 = (now - sma20) / sma20
    price_vs_sma50 = (now - sma50) / sma50

    # Volatility 20d: std of daily log-returns over last 20 candles
    import math
    log_returns    = [math.log(closes[i]/closes[i-1]) for i in range(-20, 0)]
    volatility_20d = statistics.stdev(log_returns) if len(log_returns) > 1 else 0.05

    # Price change 1h: compare now vs 4 candles ago (4 x 15m = 1h)
    price_change_1h = (now - closes[-5]) / closes[-5] if len(closes) >= 5 else 0.0

    # Bid-ask spread proxy: average (high-low)/close over last 5 candles
    bid_ask_spread = sum(
        (highs[i] - lows[i]) / closes[i] for i in range(-5, 0)
    ) / 5

    return {
        "rsi_14":          rsi,
        "macd":            macd,
        "price_vs_sma20":  price_vs_sma20,
        "price_vs_sma50":  price_vs_sma50,
        "price_change_1h": price_change_1h,
        "volatility_20d":  volatility_20d,
        "bid_ask_spread":  bid_ask_spread,
        "price_history":   closes[-20:],
    }
