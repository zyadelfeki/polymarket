import asyncio
import json
import websockets
from typing import Dict, Callable, Optional
from datetime import datetime
import logging
from collections import deque

logger = logging.getLogger(__name__)

class BinanceWebSocketFeed:
    def __init__(self):
        self.ws_url = "wss://stream.binance.com:9443/ws"
        self.subscriptions = ["btcusdt@ticker", "ethusdt@ticker", "solusdt@ticker"]
        self.prices = {
            "BTC": {"price": 0.0, "timestamp": None, "change_1m": 0.0},
            "ETH": {"price": 0.0, "timestamp": None, "change_1m": 0.0},
            "SOL": {"price": 0.0, "timestamp": None, "change_1m": 0.0}
        }
        self.price_history = {
            "BTC": deque(maxlen=60),
            "ETH": deque(maxlen=60),
            "SOL": deque(maxlen=60)
        }
        self.volatility_windows = {
            "BTC": {"current": 0.0, "spike_detected": False},
            "ETH": {"current": 0.0, "spike_detected": False},
            "SOL": {"current": 0.0, "spike_detected": False}
        }
        self.on_price_update = None
        self.on_volatility_spike = None
        self.running = False
        self.websocket = None
    
    async def connect(self):
        streams = "/".join(self.subscriptions)
        url = f"{self.ws_url}/{streams}"
        try:
            self.websocket = await websockets.connect(url)
            self.running = True
            logger.info(f"✅ Binance WebSocket connected")
            return True
        except Exception as e:
            logger.error(f"❌ WebSocket connection failed: {e}")
            return False
    
    async def listen(self):
        if not self.websocket:
            await self.connect()
        try:
            while self.running:
                message = await self.websocket.recv()
                data = json.loads(message)
                symbol = data.get("s", "")
                price = float(data.get("c", 0))
                price_change_pct = float(data.get("P", 0))
                
                if "BTC" in symbol:
                    await self._update_price("BTC", price, price_change_pct)
                elif "ETH" in symbol:
                    await self._update_price("ETH", price, price_change_pct)
                elif "SOL" in symbol:
                    await self._update_price("SOL", price, price_change_pct)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("⚠️  WebSocket closed, reconnecting...")
            await asyncio.sleep(2)
            await self.connect()
            await self.listen()
        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}")
            self.running = False
    
    async def _update_price(self, symbol: str, price: float, change_pct: float):
        now = datetime.utcnow()
        old_price = self.prices[symbol]["price"]
        self.prices[symbol] = {
            "price": price,
            "timestamp": now,
            "change_24h": change_pct
        }
        self.price_history[symbol].append({"price": price, "timestamp": now})
        
        if len(self.price_history[symbol]) >= 60:
            prices_1m = [p["price"] for p in list(self.price_history[symbol])[-60:]]
            volatility_1m = self._calculate_volatility(prices_1m)
            self.volatility_windows[symbol]["current"] = volatility_1m
            
            if volatility_1m > 3.0 and not self.volatility_windows[symbol]["spike_detected"]:
                self.volatility_windows[symbol]["spike_detected"] = True
                logger.warning(f"🚨 VOLATILITY SPIKE: {symbol} moved {volatility_1m:.2f}% in 60s")
                if self.on_volatility_spike:
                    await self.on_volatility_spike(symbol, volatility_1m, price)
            elif volatility_1m < 1.5:
                self.volatility_windows[symbol]["spike_detected"] = False
        
        if old_price > 0:
            instant_change = ((price - old_price) / old_price) * 100
            self.prices[symbol]["change_instant"] = instant_change
            if abs(instant_change) > 0.5:
                logger.info(f"⚡ {symbol} instant move: {instant_change:+.2f}% → ${price:,.2f}")
        
        if self.on_price_update:
            await self.on_price_update(symbol, price, self.prices[symbol])
    
    def _calculate_volatility(self, prices: list) -> float:
        if len(prices) < 2:
            return 0.0
        max_price = max(prices)
        min_price = min(prices)
        return ((max_price - min_price) / min_price) * 100
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        return self.prices.get(symbol, {}).get("price")
    
    def get_volatility(self, symbol: str) -> float:
        return self.volatility_windows.get(symbol, {}).get("current", 0.0)
    
    def is_volatile(self, symbol: str, threshold: float = 3.0) -> bool:
        return self.get_volatility(symbol) > threshold
    
    async def close(self):
        self.running = False
        if self.websocket:
            await self.websocket.close()