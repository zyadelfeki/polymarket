import asyncio
import sys
sys.path.append('.')

from data_feeds.polymarket_client import PolymarketClient
from config.settings import settings

async def test_polymarket():
    print("\n" + "="*60)
    print("TESTING POLYMARKET CLIENT")
    print("="*60 + "\n")
    
    client = PolymarketClient()
    
    print(f"Mode: {'PAPER TRADING' if settings.PAPER_TRADING else 'LIVE TRADING'}")
    print(f"Can Trade: {client.can_trade}")
    print(f"Address: {client.address or 'Not configured'}")
    print()
    
    print("Test 1: Fetching Markets...")
    try:
        markets = await client.get_markets(limit=10)
        print(f"✅ Fetched {len(markets)} markets")
        
        if markets:
            print("\nSample market:")
            m = markets[0]
            print(f"  Question: {m.get('question', 'N/A')}")
            print(f"  ID: {m.get('condition_id', 'N/A')[:20]}...")
            print(f"  Active: {m.get('active', 'N/A')}")
    except Exception as e:
        print(f"❌ Market fetch failed: {e}")
        return False
    
    print("\nTest 2: Scanning Crypto Markets...")
    try:
        crypto_markets = await client.scan_crypto_markets_parallel()
        print(f"✅ Found {len(crypto_markets)} crypto markets")
        
        if crypto_markets:
            print("\nSample crypto markets:")
            for m in crypto_markets[:3]:
                print(f"  - {m.get('question', 'N/A')[:60]}...")
    except Exception as e:
        print(f"❌ Crypto scan failed: {e}")
        return False
    
    if crypto_markets:
        print("\nTest 3: Fetching Market Prices...")
        try:
            price_data = await client.get_market_prices_parallel(crypto_markets[:3])
            print(f"✅ Fetched prices for {len(price_data)} markets")
            
            for condition_id, data in list(price_data.items())[:2]:
                print(f"\n  Market: {data['market_title'][:50]}...")
                print(f"  YES: ${data['yes_price']:.3f} | NO: ${data['no_price']:.3f}")
                print(f"  Liquidity: ${data['total_liquidity']:.2f}")
        except Exception as e:
            print(f"❌ Price fetch failed: {e}")
            return False
    
    print("\n" + "="*60)
    print("✅ ALL POLYMARKET TESTS PASSED")
    print("="*60 + "\n")
    
    return True

if __name__ == "__main__":
    result = asyncio.run(test_polymarket())
    sys.exit(0 if result else 1)