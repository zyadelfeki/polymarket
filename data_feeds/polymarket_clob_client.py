#!/usr/bin/env python3
"""
Real py-clob-client Integration - NO PLACEHOLDERS

Properly uses py-clob-client for:
1. Order placement (buy/sell)
2. Orderbook fetching
3. Market data
4. Balance checking
"""

import logging
from typing import Dict, List, Optional
from decimal import Decimal
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import os

logger = logging.getLogger(__name__)

class PolymarketCLOBClient:
    """
    Real Polymarket CLOB client using py-clob-client
    """
    
    def __init__(self, private_key: Optional[str] = None):
        self.private_key = private_key or os.getenv('POLYMARKET_PRIVATE_KEY')
        
        if not self.private_key:
            raise ValueError("POLYMARKET_PRIVATE_KEY not set")
        
        # Initialize py-clob-client
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            key=self.private_key,
            chain_id=137  # Polygon mainnet
        )
        
        # Create and set API credentials
        try:
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            logger.info("✅ Polymarket CLOB client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize CLOB client: {e}", exc_info=True)
            raise
    
    def get_orderbook(self, token_id: str) -> Dict:
        """
        Get orderbook for a specific token
        
        Returns:
        {
            'market': str,
            'asset_id': str,
            'bids': [{'price': str, 'size': str}, ...],
            'asks': [{'price': str, 'size': str}, ...],
            'timestamp': int
        }
        """
        try:
            orderbook = self.client.get_order_book(token_id)
            return orderbook
        except Exception as e:
            logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
            return {'bids': [], 'asks': []}
    
    def calculate_orderbook_depth(self, token_id: str, levels: int = 10) -> Dict:
        """
        Calculate total liquidity in top N levels of orderbook
        
        Returns: {'bid_depth': float, 'ask_depth': float, 'total_depth': float}
        """
        orderbook = self.get_orderbook(token_id)
        
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        bid_depth = sum(
            float(order.get('size', 0)) * float(order.get('price', 0))
            for order in bids[:levels]
        )
        
        ask_depth = sum(
            float(order.get('size', 0)) * float(order.get('price', 0))
            for order in asks[:levels]
        )
        
        return {
            'bid_depth': bid_depth,
            'ask_depth': ask_depth,
            'total_depth': bid_depth + ask_depth,
            'bid_levels': len(bids),
            'ask_levels': len(asks)
        }
    
    def get_best_bid_ask(self, token_id: str) -> Dict:
        """
        Get best bid and ask price
        
        Returns: {'bid': float, 'ask': float, 'spread': float}
        """
        orderbook = self.get_orderbook(token_id)
        
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        best_bid = float(bids[0]['price']) if bids else 0.0
        best_ask = float(asks[0]['price']) if asks else 1.0
        spread = best_ask - best_bid
        
        return {
            'bid': best_bid,
            'ask': best_ask,
            'spread': spread,
            'spread_pct': spread / best_ask if best_ask > 0 else 0
        }
    
    def place_order(self,
                   token_id: str,
                   side: str,  # 'BUY' or 'SELL'
                   price: float,
                   size: float,
                   order_type: str = 'GTC') -> Optional[Dict]:
        """
        Place an order on Polymarket CLOB
        
        Args:
            token_id: Token ID (e.g., '16678291189211314787145083999015737376658799626183230671758641503291735614088')
            side: 'BUY' or 'SELL'
            price: Limit price (0.01 to 0.99)
            size: Number of shares
            order_type: 'GTC' (Good Till Cancel), 'FOK' (Fill or Kill), 'GTD' (Good Till Date)
        
        Returns: Order result dict or None on failure
        """
        try:
            # Create order
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side.upper() == 'BUY' else SELL
            )
            
            # Sign and post order
            signed_order = self.client.create_order(order_args)
            
            order_type_enum = OrderType.GTC
            if order_type.upper() == 'FOK':
                order_type_enum = OrderType.FOK
            elif order_type.upper() == 'GTD':
                order_type_enum = OrderType.GTD
            
            result = self.client.post_order(signed_order, order_type_enum)
            
            if result.get('success'):
                logger.info(
                    f"✅ Order placed: {side} {size} @ ${price:.3f} on {token_id[:20]}..."
                )
            else:
                logger.error(f"❌ Order failed: {result.get('error', 'Unknown error')}")
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to place order: {e}", exc_info=True)
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order
        """
        try:
            result = self.client.cancel(order_id)
            return result.get('success', False)
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    def get_open_orders(self) -> List[Dict]:
        """
        Get all open orders for this account
        """
        try:
            orders = self.client.get_orders()
            return orders
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []
    
    def get_balance(self) -> Dict:
        """
        Get USDC balance
        
        Returns: {'balance': float}
        """
        try:
            balance_info = self.client.get_balance()
            return balance_info
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return {'balance': 0.0}
    
    def market_buy(self, token_id: str, amount_usd: float) -> Optional[Dict]:
        """
        Market buy (buy at best ask)
        """
        best_prices = self.get_best_bid_ask(token_id)
        best_ask = best_prices['ask']
        
        size = amount_usd / best_ask if best_ask > 0 else 0
        
        return self.place_order(
            token_id=token_id,
            side='BUY',
            price=best_ask,
            size=size,
            order_type='FOK'  # Fill or kill for market orders
        )
    
    def market_sell(self, token_id: str, size: float) -> Optional[Dict]:
        """
        Market sell (sell at best bid)
        """
        best_prices = self.get_best_bid_ask(token_id)
        best_bid = best_prices['bid']
        
        return self.place_order(
            token_id=token_id,
            side='SELL',
            price=best_bid,
            size=size,
            order_type='FOK'
        )


# Example usage
if __name__ == '__main__':
    import os
    
    # Test with your private key
    private_key = os.getenv('POLYMARKET_PRIVATE_KEY')
    
    if not private_key:
        print("Set POLYMARKET_PRIVATE_KEY environment variable")
        exit(1)
    
    client = PolymarketCLOBClient(private_key)
    
    # Example token ID
    token_id = "16678291189211314787145083999015737376658799626183230671758641503291735614088"
    
    # Get orderbook
    orderbook = client.get_orderbook(token_id)
    print(f"Orderbook bids: {len(orderbook.get('bids', []))}")
    print(f"Orderbook asks: {len(orderbook.get('asks', []))}")
    
    # Get best prices
    prices = client.get_best_bid_ask(token_id)
    print(f"Best bid: ${prices['bid']:.3f}")
    print(f"Best ask: ${prices['ask']:.3f}")
    print(f"Spread: ${prices['spread']:.4f} ({prices['spread_pct']:.2%})")
    
    # Get balance
    balance = client.get_balance()
    print(f"Balance: ${balance.get('balance', 0):.2f}")
