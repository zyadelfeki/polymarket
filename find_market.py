#!/usr/bin/env python3
"""
Market Discovery Script - Find Bitcoin/100k Market IDs

This script searches Polymarket for Bitcoin-related markets
and prints their IDs, slugs, and questions to help identify
the correct market for trading.
"""

import asyncio
import os
import sys
from dotenv import load_dotenv
import structlog

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from data_feeds.polymarket_client_v2 import PolymarketClientV2

# Configure simple console logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer()
    ]
)

logger = structlog.get_logger(__name__)


async def find_bitcoin_markets():
    """Search for Bitcoin-related markets on Polymarket."""
    
    print("\n" + "="*80)
    print("POLYMARKET MARKET DISCOVERY - BITCOIN MARKETS")
    print("="*80 + "\n")
    
    # Load environment
    load_dotenv()
    
    # Initialize client (paper trading mode, no private key needed for read-only)
    client = PolymarketClientV2(
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        paper_trading=True
    )
    
    try:
        print("Fetching markets from Polymarket...")
        
        # Get markets WITHOUT active filter to see everything
        # The active=False returns both active and closed markets
        markets = await client.get_markets(active=False, limit=1000)
        
        print(f"Total markets fetched: {len(markets)}\n")
        
        # Search for Bitcoin-related markets
        bitcoin_keywords = ['bitcoin', 'btc', '100k', '100,000', '100000']
        
        matching_markets = []
        
        for market in markets:
            if not isinstance(market, dict):
                continue
            
            question = str(market.get('question', '')).lower()
            slug = str(market.get('slug', '')).lower()
            description = str(market.get('description', '')).lower()
            
            # Check if any Bitcoin keyword is in the market data
            if any(keyword in question or keyword in slug or keyword in description 
                   for keyword in bitcoin_keywords):
                matching_markets.append(market)
        
        print(f"Found {len(matching_markets)} Bitcoin-related markets:\n")
        print("-"*80)
        
        # Print detailed information for each match
        for i, market in enumerate(matching_markets, 1):
            print(f"\n[{i}] Market Found:")
            print(f"  Question:      {market.get('question', 'N/A')}")
            print(f"  Slug:          {market.get('slug', 'N/A')}")
            print(f"  ID:            {market.get('id', 'N/A')}")
            print(f"  Condition ID:  {market.get('condition_id', 'N/A')}")
            print(f"  Active:        {market.get('active', 'N/A')}")
            print(f"  Closed:        {market.get('closed', False)}")
            print(f"  End Date:      {market.get('end_date_iso', 'N/A')}")
            
            # Print token information
            tokens = market.get('tokens', [])
            if tokens:
                print(f"  Tokens ({len(tokens)}):")
                for token in tokens:
                    token_id = token.get('token_id', 'N/A')
                    outcome = token.get('outcome', 'N/A')
                    price = token.get('price', 'N/A')
                    print(f"    - {outcome}: token_id={token_id}, price={price}")
            else:
                print("  Tokens:        None")
            
            print("-"*80)
        
        if not matching_markets:
            print("\n⚠️  No Bitcoin-related markets found in first 1000 markets.")
            print("\nShowing first 20 markets for reference:\n")
            print("-"*80)
            
            for i, market in enumerate(markets[:20], 1):
                print(f"\n[{i}]")
                print(f"  Question: {market.get('question', 'N/A')}")
                print(f"  Slug:     {market.get('slug', 'N/A')}")
                print(f"  ID:       {market.get('id', 'N/A')}")
                print(f"  Closed:   {market.get('closed', False)}")
        
        print("\n" + "="*80)
        print("SEARCH COMPLETE")
        print("="*80 + "\n")
        
        # Print usage instructions
        if matching_markets:
            print("To use a market, copy its 'slug', 'id', or 'condition_id' and update your config.")
            print("Example: Set MARKET_ID='<slug>' in your .env or config file.\n")
        else:
            print("💡 TIP: The 'BTC to 100k' market might not be in the first 1000 results.")
            print("Try searching on polymarket.com directly and copy the market slug from the URL.\n")
            print("Example URL: https://polymarket.com/event/bitcoin-to-100k-by-end-of-2024")
            print("Market slug: bitcoin-to-100k-by-end-of-2024\n")
        
    except Exception as e:
        logger.error("market_search_failed", error=str(e), exc_info=True)
        print(f"\nERROR: {e}\n")
        return 1
    
    finally:
        await client.close()
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(find_bitcoin_markets())
    sys.exit(exit_code)
