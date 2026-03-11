import asyncio
import sys
sys.path.append('.')

from data_feeds.binance_websocket import BinanceWebSocketFeed

async def run_binance():
    print("\n" + "="*60)
    print("TESTING BINANCE WEBSOCKET")
    print("="*60 + "\n")
    
    feed = BinanceWebSocketFeed()
    
    update_count = {"BTC": 0, "ETH": 0, "SOL": 0}
    
    async def on_price(symbol, price, data):
        update_count[symbol] += 1
        volatility = feed.get_volatility(symbol)
        print(f"{symbol}: ${price:,.2f} | 1m Vol: {volatility:.2f}% | Updates: {update_count[symbol]}")
    
    async def on_spike(symbol, volatility, price):
        print(f"\n⚠️  VOLATILITY SPIKE DETECTED: {symbol}")
        print(f"   Price: ${price:,.2f}")
        print(f"   60s Volatility: {volatility:.2f}%\n")
    
    feed.on_price_update = on_price
    feed.on_volatility_spike = on_spike
    
    connected = await feed.connect()
    
    if connected:
        print("✅ Connected successfully")
        print("📊 Listening for price updates (30 seconds test)...\n")
        
        try:
            await asyncio.wait_for(feed.listen(), timeout=30)
        except asyncio.TimeoutError:
            print("\n" + "="*60)
            print("TEST COMPLETE")
            print("="*60)
            print("\nPrice Updates Received:")
            for symbol, count in update_count.items():
                print(f"  {symbol}: {count} updates")
                print(f"  Current Price: ${feed.get_current_price(symbol):,.2f}")
                print(f"  Volatility: {feed.get_volatility(symbol):.2f}%")
            print("\n✅ Binance WebSocket test PASSED")
        finally:
            await feed.close()
    else:
        print("❌ Connection failed")
        return False
    
    return True

if __name__ == "__main__":
    result = asyncio.run(run_binance())
    sys.exit(0 if result else 1)