"""
Binance WebSocket Feed
Real-time cryptocurrency price updates with millisecond latency
"""
import asyncio
import json
import websockets
from typing import Dict, Callable, Optional, List
from datetime import datetime
from collections import deque
import logging

logger = logging.getLogger(__name__)

class BinanceWebSocketFeed:
    """Ultra-low latency price feed via WebSocket"""
    
    def __init__(self, symbols: List[str] = ["BTC", "ETH", "SOL"]):
        self.ws_url = "wss://stream.binance.com:9443/ws"
        self.symbols = symbols
        
        # Build subscription list
        symbol_map = {"BTC": "btcusdt", "ETH": "ethusdt", "SOL": "solusdt"}
        self.subscriptions = [f"{symbol_map[s]}@ticker" for s in symbols if s in symbol_map]
        
        # Price storage
        self.prices = {s: {"price": 0.0, "timestamp": None, "change_1m": 0.0} for s in symbols}
        
        # 60-second price history for volatility calculation
        self.price_history = {s: deque(maxlen=60) for s in symbols}
        
        # Volatility tracking
        self.volatility = {s: {"current": 0.0, "spike_active": False} for s in symbols}
        
        # Callbacks
        self.on_price_update: Optional[Callable] = None
        self.on_volatility_spike: Optional[Callable] = None
        
        self.running = False
        self.websocket = None
    
    async def connect(self) -> bool:
        """Establish WebSocket connection"""
        try:
            streams = "/".join(self.subscriptions)
            url = f"{self.ws_url}/{streams}"
            self.websocket = await websockets.connect(url)
            self.running = True
            logger.info(f"✅ WebSocket connected: {self.symbols}")
            return True
        except Exception as e:
            logger.error(f"❌ WebSocket connection failed: {e}")
            return False
    
    async def listen(self):
        """Main event loop"""
        if not self.websocket:
            await self.connect()
        
        try:
            while self.running:
                message = await self.websocket.recv()
                data = json.loads(message)
                
                symbol_map = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"}
                exchange_symbol = data.get("s", "")
                
                if exchange_symbol in symbol_map:
                    symbol = symbol_map[exchange_symbol]
                    price = float(data.get("c", 0))
                    change_pct = float(data.get("P", 0))
                    
                    await self._process_price_update(symbol, price, change_pct)
                    
        except websockets.exceptions.ConnectionClosed:
            logger.warning("⚠️  Connection closed, reconnecting...")
            await asyncio.sleep(2)
            await self.connect()
            await self.listen()
        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}")
            self.running = False
    
    async def _process_price_update(self, symbol: str, price: float, change_24h: float):
        """Process price update and detect volatility"""
        now = datetime.utcnow()
        old_price = self.prices[symbol]["price"]
        
        self.prices[symbol] = {
            "price": price,
            "timestamp": now,
            "change_24h": change_24h
        }
        
        # Add to history
        self.price_history[symbol].append({"price": price, "timestamp": now})
        
        # Calculate 1-minute volatility
        if len(self.price_history[symbol]) >= 60:
            recent_prices = [p["price"] for p in list(self.price_history[symbol])[-60:]]
            volatility_pct = self._calculate_volatility(recent_prices)
            self.volatility[symbol]["current"] = volatility_pct
            
            # Detect spike
            if volatility_pct > 3.0 and not self.volatility[symbol]["spike_active"]:
                self.volatility[symbol]["spike_active"] = True
                logger.warning(f"🚨 {symbol} VOLATILITY SPIKE: {volatility_pct:.2f}%")
                
                if self.on_volatility_spike:
                    await self.on_volatility_spike(symbol, volatility_pct, price)
            
            elif volatility_pct < 1.5:
                self.volatility[symbol]["spike_active"] = False
        
        # Instant change detection
        if old_price > 0:
            instant_change = ((price - old_price) / old_price) * 100
            if abs(instant_change) > 0.5:
                logger.debug(f"⚡ {symbol} moved {instant_change:+.2f}%")
        
        if self.on_price_update:
            await self.on_price_update(symbol, price, self.prices[symbol])
    
    def _calculate_volatility(self, prices: List[float]) -> float:
        """Calculate percentage volatility"""
        if len(prices) < 2:
            return 0.0
        max_p = max(prices)
        min_p = min(prices)
        return ((max_p - min_p) / min_p) * 100
    
    def get_price(self, symbol: str) -> Optional[float]:
        """Get current price"""
        return self.prices.get(symbol, {}).get("price")
    
    def get_volatility(self, symbol: str) -> float:
        """Get current volatility"""
        return self.volatility.get(symbol, {}).get("current", 0.0)
    
    async def close(self):
        """Close connection"""
        self.running = False
        if self.websocket:
            await self.websocket.close()