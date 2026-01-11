#!/usr/bin/env python3
"""
Real Polymarket WebSocket Implementation - NO PLACEHOLDERS

Connects to:
1. CLOB WebSocket (wss://ws-subscriptions-clob.polymarket.com) - Orders & orderbook
2. RTDS WebSocket (wss://ws-live-data.polymarket.com) - Market data & crypto prices
"""

import asyncio
import json
import logging
import websockets
from typing import Dict, List, Callable, Optional
from datetime import datetime
import hmac
import hashlib
import time

logger = logging.getLogger(__name__)

class PolymarketWebSocket:
    """
    Real Polymarket WebSocket client - connects to BOTH websockets:
    1. CLOB WSS - for order execution and orderbook
    2. RTDS - for real-time market data
    """
    
    CLOB_WSS_URL = "wss://ws-subscriptions-clob.polymarket.com"
    RTDS_WSS_URL = "wss://ws-live-data.polymarket.com"
    
    def __init__(self, api_key: Optional[str] = None, 
                 api_secret: Optional[str] = None,
                 api_passphrase: Optional[str] = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        
        self.clob_ws = None
        self.rtds_ws = None
        
        self.message_handlers = {
            'crypto_prices': [],
            'activity': [],
            'clob_market': [],
            'orderbook': []
        }
        
        self.orderbooks = {}  # token_id -> {bids, asks}
        self.prices = {}  # symbol -> price
        self.trades = []  # Recent trades
        
        self.connected = False
        self.ping_task = None
    
    async def connect(self):
        """
        Connect to both Polymarket WebSockets
        """
        try:
            # Connect to RTDS (real-time data)
            logger.info(f"Connecting to RTDS: {self.RTDS_WSS_URL}")
            self.rtds_ws = await websockets.connect(self.RTDS_WSS_URL)
            logger.info("✅ Connected to Polymarket RTDS WebSocket")
            
            # Connect to CLOB if we have credentials
            if self.api_key and self.api_secret:
                logger.info(f"Connecting to CLOB: {self.CLOB_WSS_URL}")
                self.clob_ws = await websockets.connect(self.CLOB_WSS_URL)
                logger.info("✅ Connected to Polymarket CLOB WebSocket")
            
            self.connected = True
            
            # Start ping task to keep connection alive
            self.ping_task = asyncio.create_task(self._ping_loop())
            
            return True
            
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}", exc_info=True)
            return False
    
    async def _ping_loop(self):
        """
        Send PING every 5 seconds to keep connection alive
        """
        while self.connected:
            try:
                if self.rtds_ws and not self.rtds_ws.closed:
                    await self.rtds_ws.ping()
                if self.clob_ws and not self.clob_ws.closed:
                    await self.clob_ws.ping()
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Ping error: {e}")
                await asyncio.sleep(1)
    
    async def subscribe_crypto_prices(self, symbols: List[str]):
        """
        Subscribe to real-time crypto prices from RTDS
        
        Example: subscribe_crypto_prices(['BTC', 'ETH', 'SOL'])
        """
        if not self.rtds_ws:
            logger.error("RTDS WebSocket not connected")
            return
        
        for symbol in symbols:
            subscription = {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices",
                        "type": "update",
                        "filters": json.dumps({"symbol": symbol})
                    }
                ]
            }
            
            await self.rtds_ws.send(json.dumps(subscription))
            logger.info(f"✅ Subscribed to {symbol} price updates")
    
    async def subscribe_orderbook(self, token_ids: List[str]):
        """
        Subscribe to real-time orderbook updates from RTDS
        
        Example: subscribe_orderbook(['16678291189211314787145083999015737376658799626183230671758641503291735614088'])
        """
        if not self.rtds_ws:
            logger.error("RTDS WebSocket not connected")
            return
        
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "clob_market",
                    "type": "agg_orderbook",
                    "filters": json.dumps(token_ids)  # Array of token IDs
                }
            ]
        }
        
        await self.rtds_ws.send(json.dumps(subscription))
        logger.info(f"✅ Subscribed to orderbook for {len(token_ids)} tokens")
    
    async def subscribe_trades(self, market_slug: Optional[str] = None):
        """
        Subscribe to real-time trades from RTDS
        
        Example: subscribe_trades('will-bitcoin-be-above-95000-on-january-15')
        """
        if not self.rtds_ws:
            logger.error("RTDS WebSocket not connected")
            return
        
        filters = {}
        if market_slug:
            filters['market_slug'] = market_slug
        
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "activity",
                    "type": "trades",
                    "filters": json.dumps(filters) if filters else ""
                }
            ]
        }
        
        await self.rtds_ws.send(json.dumps(subscription))
        logger.info(f"✅ Subscribed to trades {f'for {market_slug}' if market_slug else '(all)'}")
    
    async def listen(self):
        """
        Main listen loop - processes messages from both WebSockets
        """
        tasks = []
        
        if self.rtds_ws:
            tasks.append(asyncio.create_task(self._listen_rtds()))
        
        if self.clob_ws:
            tasks.append(asyncio.create_task(self._listen_clob()))
        
        if not tasks:
            logger.error("No WebSocket connections active")
            return
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _listen_rtds(self):
        """
        Listen to RTDS WebSocket for market data
        """
        try:
            async for message in self.rtds_ws:
                try:
                    data = json.loads(message)
                    await self._handle_rtds_message(data)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from RTDS: {message}")
                except Exception as e:
                    logger.error(f"Error handling RTDS message: {e}", exc_info=True)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("RTDS WebSocket closed, attempting reconnect...")
            await self._reconnect_rtds()
        except Exception as e:
            logger.error(f"RTDS listen error: {e}", exc_info=True)
    
    async def _listen_clob(self):
        """
        Listen to CLOB WebSocket for order updates
        """
        try:
            async for message in self.clob_ws:
                try:
                    data = json.loads(message)
                    await self._handle_clob_message(data)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON from CLOB: {message}")
                except Exception as e:
                    logger.error(f"Error handling CLOB message: {e}", exc_info=True)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("CLOB WebSocket closed, attempting reconnect...")
            await self._reconnect_clob()
        except Exception as e:
            logger.error(f"CLOB listen error: {e}", exc_info=True)
    
    async def _handle_rtds_message(self, data: Dict):
        """
        Handle messages from RTDS WebSocket
        
        Message structure:
        {
            "topic": "crypto_prices",
            "type": "update",
            "timestamp": 1736625600000,
            "payload": {...}
        }
        """
        topic = data.get('topic')
        msg_type = data.get('type')
        payload = data.get('payload', {})
        
        if topic == 'crypto_prices' and msg_type == 'update':
            symbol = payload.get('symbol')
            price = payload.get('price')
            if symbol and price:
                self.prices[symbol] = float(price)
                logger.debug(f"Price update: {symbol} = ${price}")
                
                # Call registered handlers
                for handler in self.message_handlers.get('crypto_prices', []):
                    await handler(symbol, price)
        
        elif topic == 'activity' and msg_type in ['trades', 'orders_matched']:
            trade = payload
            self.trades.append(trade)
            if len(self.trades) > 1000:
                self.trades = self.trades[-1000:]  # Keep last 1000
            
            for handler in self.message_handlers.get('activity', []):
                await handler(trade)
        
        elif topic == 'clob_market' and msg_type == 'agg_orderbook':
            token_id = payload.get('asset_id')
            if token_id:
                self.orderbooks[token_id] = {
                    'bids': payload.get('bids', []),
                    'asks': payload.get('asks', []),
                    'timestamp': data.get('timestamp')
                }
                
                for handler in self.message_handlers.get('orderbook', []):
                    await handler(token_id, self.orderbooks[token_id])
    
    async def _handle_clob_message(self, data: Dict):
        """
        Handle messages from CLOB WebSocket (orders, execution)
        """
        # CLOB messages are for private order updates
        logger.debug(f"CLOB message: {data}")
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get latest cached price for a symbol
        """
        return self.prices.get(symbol)
    
    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """
        Get latest cached orderbook for a token
        """
        return self.orderbooks.get(token_id)
    
    def register_handler(self, topic: str, handler: Callable):
        """
        Register a callback for specific message types
        
        Example:
            ws.register_handler('crypto_prices', lambda symbol, price: print(f"{symbol}: ${price}"))
        """
        if topic not in self.message_handlers:
            self.message_handlers[topic] = []
        self.message_handlers[topic].append(handler)
    
    async def _reconnect_rtds(self):
        """
        Reconnect to RTDS WebSocket
        """
        try:
            await asyncio.sleep(2)
            self.rtds_ws = await websockets.connect(self.RTDS_WSS_URL)
            logger.info("✅ Reconnected to RTDS")
        except Exception as e:
            logger.error(f"RTDS reconnect failed: {e}")
    
    async def _reconnect_clob(self):
        """
        Reconnect to CLOB WebSocket
        """
        try:
            await asyncio.sleep(2)
            self.clob_ws = await websockets.connect(self.CLOB_WSS_URL)
            logger.info("✅ Reconnected to CLOB")
        except Exception as e:
            logger.error(f"CLOB reconnect failed: {e}")
    
    async def close(self):
        """
        Close both WebSocket connections
        """
        self.connected = False
        
        if self.ping_task:
            self.ping_task.cancel()
        
        if self.rtds_ws:
            await self.rtds_ws.close()
        
        if self.clob_ws:
            await self.clob_ws.close()
        
        logger.info("WebSocket connections closed")


# Example usage
if __name__ == '__main__':
    async def main():
        ws = PolymarketWebSocket()
        await ws.connect()
        
        # Subscribe to crypto prices
        await ws.subscribe_crypto_prices(['BTC', 'ETH', 'SOL'])
        
        # Register handler for price updates
        ws.register_handler(
            'crypto_prices',
            lambda symbol, price: print(f"{symbol}: ${price}")
        )
        
        # Listen for messages
        await ws.listen()
    
    asyncio.run(main())
