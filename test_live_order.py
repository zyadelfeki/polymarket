import os
import asyncio
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

async def main():
    load_dotenv(override=True)
    
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("ERROR: Missing Private Key")
        return

    print("--- 1. AUTHENTICATING ---")
    host = "https://clob.polymarket.com"
    chain_id = 137
    
    try:
        client = ClobClient(host, key=key, chain_id=chain_id)
        # Try both auth methods to be safe
        try:
            creds = client.create_or_derive_api_creds()
        except AttributeError:
            creds = client.create_or_derive_api_key()
            
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        print("✅ Authenticated")
    except Exception as e:
        print(f"❌ Auth Failed: {e}")
        return

    # 2. Define the Trade (Bitcoin > 94k)
    condition_id = "0xd3460cd313aa9759ea67a966e9a499cb65964d6e2a2ff6902472aa83005383bb"
    
    print(f"--- 2. FETCHING MARKET ---")
    try:
        market = client.get_market(condition_id)
        token_id = market['tokens'][1]['token_id']  # Token 1 is "NO"
        print(f"✅ Market Found. Buying 'NO' Token: {token_id[:10]}...")
    except Exception as e:
        print(f"❌ Market Fetch Failed: {e}")
        return

    # 3. Place Limit Order
    print("--- 3. SENDING LIVE ORDER ---")
    
    try:
        order_args = OrderArgs(
            price=0.01,
            size=1.0,
            side="BUY", 
            token_id=token_id
        )
        resp = client.create_and_post_order(order_args)
        print("\n🎉 SUCCESS! ORDER PLACED:")
        print(f"Order ID: {resp.get('orderID')}")
        print("Check 'Activity' on Polymarket.com!")
    except Exception as e:
        print("\n❌ ORDER REJECTED:")
        print(e)

if __name__ == "__main__":
    asyncio.run(main())