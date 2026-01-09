import asyncio
import json
import websockets
from typing import Dict, Callable, Optional
from datetime import datetime
import logging
from collections import deque
from config.settings import settings
from config.markets import CRYPTO_SYMBOLS

logger = logging.getLogger(__name__)

class BinanceWebSocketFeed:
    def __init__(self):
        self.ws_url = "wss://stream.binance.com:9443/ws"
        self.subscriptions = [v["binance_symbol"].lower() + "@ticker" for v in CRYPTO_SYMBOLS.values()]
        
        self.prices = {symbol: {"price": 0.0, "timestamp": None, "change_1m": 0.0, "change_24h": 0.0} for symbol in CRYPTO_SYMBOLS.keys()}
        self.price_history = {symbol: deque(maxlen=60) for symbol in CRYPTO_SYMBOLS.keys()}
        self.volatility_windows = {symbol: {"current": 0.0, "spike_detected": False, "last_spike": None} for symbol in CRYPTO_SYMBOLS.keys()}
        
        self.on_price_update = None
        self.on_volatility_spike = None
        
        self.running = False
        self.websocket = None
        self.reconnect_delay = 2
    
    async def connect(self):
        streams = "/".join(self.subscriptions)
        url = f"{self.ws_url}/{streams}"
        
        try:
            self.websocket = await websockets.connect(url)
            self.running = True
            logger.info(f"Binance WebSocket connected: {len(self.subscriptions)} streams")
            return True
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            return False
    
    async def listen(self):
        if not self.websocket:
            await self.connect()
        
        while self.running:
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                
                symbol = data.get("s", "")
                price = float(data.get("c", 0))
                price_change_24h = float(data.get("P", 0))
                
                for key, config in CRYPTO_SYMBOLS.items():
                    if config["binance_symbol"] == symbol:
                        await self._update_price(key, price, price_change_24h)
                        break
                        
            except asyncio.TimeoutError:
                logger.warning("WebSocket timeout, sending ping")
                try:
                    await self.websocket.ping()
                except:
                    logger.error("Ping failed, reconnecting")
                    await self._reconnect()
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket closed, reconnecting")
                await self._reconnect()
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(1)
    
    async def _reconnect(self):
        self.running = False
        if self.websocket:
            await self.websocket.close()
        await asyncio.sleep(self.reconnect_delay)
        await self.connect()
        self.running = True
    
    async def _update_price(self, symbol: str, price: float, change_24h: float):
        now = datetime.utcnow()
        old_price = self.prices[symbol]["price"]
        
        self.prices[symbol] = {
            "price": price,
            "timestamp": now,
            "change_24h": change_24h,
            "change_instant": 0.0
        }
        
        self.price_history[symbol].append({"price": price, "timestamp": now})
        
        if len(self.price_history[symbol]) >= 60:
            prices_1m = [p["price"] for p in list(self.price_history[symbol])[-60:]]
            volatility_1m = self._calculate_volatility(prices_1m)
            
            self.volatility_windows[symbol]["current"] = volatility_1m
            
            threshold = CRYPTO_SYMBOLS[symbol].get("volatility_threshold", settings.VOLATILITY_SPIKE_THRESHOLD)
            
            if volatility_1m > threshold and not self.volatility_windows[symbol]["spike_detected"]:
                self.volatility_windows[symbol]["spike_detected"] = True
                self.volatility_windows[symbol]["last_spike"] = now
                
                logger.warning(f"VOLATILITY SPIKE: {symbol} moved {volatility_1m:.2f}% in 60s")
                
                if self.on_volatility_spike:
                    asyncio.create_task(self.on_volatility_spike(symbol, volatility_1m, price))
            
            elif volatility_1m < threshold * 0.5:
                self.volatility_windows[symbol]["spike_detected"] = False
        
        if old_price > 0:
            instant_change = ((price - old_price) / old_price) * 100
            self.prices[symbol]["change_instant"] = instant_change
            
            if abs(instant_change) > 0.3:
                logger.info(f"{symbol} instant move: {instant_change:+.2f}% -> ${price:,.2f}")
        
        if self.on_price_update:
            asyncio.create_task(self.on_price_update(symbol, price, self.prices[symbol]))
    
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
    
    def is_volatile(self, symbol: str) -> bool:
        threshold = CRYPTO_SYMBOLS.get(symbol, {}).get("volatility_threshold", settings.VOLATILITY_SPIKE_THRESHOLD)
        return self.get_volatility(symbol) > threshold
    
    async def close(self):
        self.running = False
        if self.websocket:
            await self.websocket.close()