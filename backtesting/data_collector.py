#!/usr/bin/env python3
"""
Historical Data Collector for Backtesting

Collects and stores:
1. Polymarket market snapshots (prices, liquidity, orderbook)
2. CEX price ticks (Binance BTC/ETH/SOL)
3. Market metadata (questions, tokens, conditions)

Storage:
- SQLite database (same as production)
- Time-series optimized queries
- Efficient retrieval for backtesting

Usage:
    collector = DataCollector()
    await collector.collect_live_data(duration_hours=24)
    
    # Later, for backtesting
    data = collector.get_historical_data(start_date, end_date)
"""

import asyncio
import sqlite3
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)

class DataCollector:
    """
    Collect and store historical data for backtesting.
    """
    
    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path
    
    @contextmanager
    def _get_connection(self):
        """Get database connection"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    
    async def collect_live_data(
        self,
        polymarket_client,
        binance_ws,
        duration_hours: int = 24,
        collection_interval: int = 15
    ):
        """
        Collect live data for specified duration.
        
        Args:
            polymarket_client: PolymarketClient instance
            binance_ws: BinanceWebSocketFeed instance
            duration_hours: How long to collect data
            collection_interval: Seconds between collections
        """
        logger.info(f"Starting data collection for {duration_hours} hours...")
        
        end_time = datetime.utcnow() + timedelta(hours=duration_hours)
        collection_count = 0
        
        while datetime.utcnow() < end_time:
            try:
                # Collect Polymarket snapshots
                await self._collect_market_snapshots(polymarket_client)
                
                # Collect CEX prices
                await self._collect_price_ticks(binance_ws)
                
                collection_count += 1
                
                if collection_count % 20 == 0:  # Every 5 minutes
                    logger.info(
                        f"Data collection progress: {collection_count} snapshots | "
                        f"Time remaining: {(end_time - datetime.utcnow()).total_seconds() / 3600:.1f}h"
                    )
                
                await asyncio.sleep(collection_interval)
            
            except Exception as e:
                logger.error(f"Error collecting data: {e}", exc_info=True)
                await asyncio.sleep(collection_interval)
        
        logger.info(f"Data collection complete: {collection_count} snapshots")
    
    async def _collect_market_snapshots(self, client):
        """
        Collect current market snapshots.
        """
        try:
            markets = await client.get_markets(limit=50)
            timestamp = datetime.utcnow()
            
            with self._get_connection() as conn:
                for market in markets:
                    condition_id = market.get('condition_id')
                    question = market.get('question')
                    tokens = market.get('tokens', [])
                    
                    if len(tokens) < 2:
                        continue
                    
                    yes_token = tokens[0]
                    no_token = tokens[1]
                    
                    # Get orderbook depth for each token
                    yes_book = await client.get_market_orderbook(yes_token.get('token_id'))
                    no_book = await client.get_market_orderbook(no_token.get('token_id'))
                    
                    # Calculate liquidity
                    yes_liquidity = sum(b['size'] for b in yes_book.get('bids', [])[:10]) if yes_book else 0
                    no_liquidity = sum(b['size'] for b in no_book.get('bids', [])[:10]) if no_book else 0
                    
                    # Store snapshot
                    conn.execute("""
                        INSERT INTO market_snapshots (
                            market_id, token_id, yes_price, no_price,
                            yes_liquidity, no_liquidity, orderbook_depth, timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        condition_id,
                        yes_token.get('token_id'),
                        float(yes_token.get('price', 0.5)),
                        float(no_token.get('price', 0.5)),
                        yes_liquidity,
                        no_liquidity,
                        json.dumps({
                            'yes_orderbook': yes_book,
                            'no_orderbook': no_book,
                            'question': question
                        }) if yes_book and no_book else None,
                        timestamp
                    ))
                
                conn.commit()
                logger.debug(f"Collected {len(markets)} market snapshots")
        
        except Exception as e:
            logger.error(f"Error collecting market snapshots: {e}")
    
    async def _collect_price_ticks(self, binance_ws):
        """
        Collect current CEX prices.
        """
        try:
            timestamp = datetime.utcnow()
            
            prices = {
                'BTC': binance_ws.get_current_price('BTC'),
                'ETH': binance_ws.get_current_price('ETH'),
                'SOL': binance_ws.get_current_price('SOL')
            }
            
            with self._get_connection() as conn:
                for symbol, price in prices.items():
                    if price:
                        conn.execute("""
                            INSERT INTO price_history (symbol, source, price, timestamp)
                            VALUES (?, ?, ?, ?)
                        """, (symbol, 'BINANCE', float(price), timestamp))
                
                conn.commit()
                logger.debug(f"Collected {len(prices)} price ticks")
        
        except Exception as e:
            logger.error(f"Error collecting price ticks: {e}")
    
    def get_historical_data(
        self,
        start_date: datetime,
        end_date: datetime,
        symbols: Optional[List[str]] = None
    ) -> Dict:
        """
        Retrieve historical data for backtesting.
        
        Args:
            start_date: Start of backtest period
            end_date: End of backtest period
            symbols: CEX symbols to include (default: ['BTC', 'ETH', 'SOL'])
        
        Returns:
            Dict with 'market_snapshots' and 'price_ticks'
        """
        symbols = symbols or ['BTC', 'ETH', 'SOL']
        
        with self._get_connection() as conn:
            # Get market snapshots
            cursor = conn.execute("""
                SELECT *
                FROM market_snapshots
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (start_date, end_date))
            
            market_snapshots = []
            for row in cursor.fetchall():
                snapshot = dict(row)
                
                # Parse orderbook JSON
                if snapshot['orderbook_depth']:
                    orderbook_data = json.loads(snapshot['orderbook_depth'])
                    snapshot['yes_orderbook'] = orderbook_data.get('yes_orderbook')
                    snapshot['no_orderbook'] = orderbook_data.get('no_orderbook')
                    snapshot['question'] = orderbook_data.get('question')
                
                market_snapshots.append(snapshot)
            
            # Get price ticks
            placeholders = ','.join('?' * len(symbols))
            cursor = conn.execute(f"""
                SELECT *
                FROM price_history
                WHERE symbol IN ({placeholders})
                  AND timestamp BETWEEN ? AND ?
                ORDER BY timestamp ASC
            """, (*symbols, start_date, end_date))
            
            price_ticks = [dict(row) for row in cursor.fetchall()]
        
        logger.info(
            f"Retrieved historical data: "
            f"{len(market_snapshots)} market snapshots, "
            f"{len(price_ticks)} price ticks"
        )
        
        return {
            'market_snapshots': market_snapshots,
            'price_ticks': price_ticks
        }
    
    def get_data_coverage(self) -> Dict:
        """
        Get summary of available historical data.
        
        Returns:
            Dict with date ranges and counts
        """
        with self._get_connection() as conn:
            # Market snapshots coverage
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as count,
                    MIN(timestamp) as first_snapshot,
                    MAX(timestamp) as last_snapshot,
                    COUNT(DISTINCT market_id) as unique_markets
                FROM market_snapshots
            """)
            market_stats = dict(cursor.fetchone())
            
            # Price ticks coverage
            cursor = conn.execute("""
                SELECT 
                    symbol,
                    COUNT(*) as count,
                    MIN(timestamp) as first_tick,
                    MAX(timestamp) as last_tick
                FROM price_history
                GROUP BY symbol
            """)
            price_stats = {row['symbol']: dict(row) for row in cursor.fetchall()}
        
        return {
            'markets': market_stats,
            'prices': price_stats
        }
    
    def generate_mock_data(
        self,
        start_date: datetime,
        end_date: datetime,
        num_markets: int = 10
    ) -> Dict:
        """
        Generate mock historical data for testing backtesting engine.
        
        Args:
            start_date: Start date
            end_date: End date
            num_markets: Number of mock markets
        
        Returns:
            Mock historical data dict
        """
        logger.info("Generating mock historical data for testing...")
        
        market_snapshots = []
        price_ticks = []
        
        current = start_date
        interval = timedelta(seconds=15)
        
        # Generate BTC price movement
        btc_price = 95000.0
        
        while current <= end_date:
            # BTC price random walk
            btc_price += (hash(str(current)) % 200) - 100  # -100 to +100
            btc_price = max(90000, min(100000, btc_price))  # Keep in range
            
            # Add price tick
            price_ticks.append({
                'symbol': 'BTC',
                'source': 'BINANCE',
                'price': btc_price,
                'timestamp': current
            })
            
            # Generate mock markets
            for i in range(num_markets):
                # Create market with threshold around current BTC price
                threshold = btc_price + (i - num_markets/2) * 500
                
                # Calculate "true" probability based on current price
                if btc_price > threshold:
                    true_yes_prob = 0.95
                else:
                    true_yes_prob = 0.05
                
                # Market price lags true probability (creates arbitrage)
                lag_factor = 0.3  # Market is 30% lagged
                yes_price = 0.5 + (true_yes_prob - 0.5) * (1 - lag_factor)
                no_price = 1.0 - yes_price
                
                market_id = f"mock_market_{i}"
                yes_token_id = f"mock_yes_{i}"
                no_token_id = f"mock_no_{i}"
                
                market_snapshots.append({
                    'market_id': market_id,
                    'token_id': yes_token_id,
                    'question': f"Will BTC close above ${threshold:,.0f}?",
                    'yes_price': yes_price,
                    'no_price': no_price,
                    'yes_liquidity': 10000,
                    'no_liquidity': 10000,
                    'timestamp': current,
                    'tokens': [
                        {'token_id': yes_token_id, 'price': yes_price},
                        {'token_id': no_token_id, 'price': no_price}
                    ]
                })
            
            current += interval
        
        logger.info(
            f"Generated mock data: "
            f"{len(price_ticks)} price ticks, "
            f"{len(market_snapshots)} market snapshots"
        )
        
        return {
            'market_snapshots': market_snapshots,
            'price_ticks': price_ticks
        }

# Standalone data collection script
async def collect_data_standalone(duration_hours: int = 24):
    """
    Standalone script to collect data for backtesting.
    
    Usage:
        python -m backtesting.data_collector
    """
    from data_feeds.polymarket_client import PolymarketClient
    from data_feeds.binance_websocket import BinanceWebSocketFeed
    
    logger.info("Starting standalone data collection...")
    
    collector = DataCollector()
    polymarket = PolymarketClient()
    binance = BinanceWebSocketFeed()
    
    # Connect Binance
    connected = await binance.connect()
    if not connected:
        logger.error("Failed to connect to Binance")
        return
    
    # Collect data
    try:
        await collector.collect_live_data(
            polymarket_client=polymarket,
            binance_ws=binance,
            duration_hours=duration_hours,
            collection_interval=15
        )
    finally:
        await binance.close()
    
    # Show coverage
    coverage = collector.get_data_coverage()
    logger.info(f"\nData Coverage:")
    logger.info(f"Markets: {coverage['markets']['count']} snapshots, "
                f"{coverage['markets']['unique_markets']} unique markets")
    logger.info(f"Date range: {coverage['markets']['first_snapshot']} to "
                f"{coverage['markets']['last_snapshot']}")
    
    for symbol, stats in coverage['prices'].items():
        logger.info(f"{symbol}: {stats['count']} ticks from "
                    f"{stats['first_tick']} to {stats['last_tick']}")

if __name__ == '__main__':
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    
    asyncio.run(collect_data_standalone(duration_hours=hours))