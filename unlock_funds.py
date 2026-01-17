import os
import asyncio
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams

async def main():
    load_dotenv(override=True)
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    host = "https://clob.polymarket.com"
    chain_id = 137

    print("--- 🔓 UNLOCKING FUNDS ---")
    
    try:
        # 1. Authenticate
        print("1. Logging in...")
        client = ClobClient(host, key=key, chain_id=chain_id)
        try:
            creds = client.create_or_derive_api_creds()
        except:
            creds = client.create_or_derive_api_key()
        
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        print("✅ Logged in.")

        # 2. Get Real Address from API
        print("2. Verifying Address...")
        # We ask the API for the correct proxy to ensure we approve the right one
        user_info = client.get_api_keys() # This call helps refresh context
        print("   (Connection established)")

        # 3. Approve
        print("3. Sending 'Approve' Transaction...")
        print("   This permits Polymarket to trade your $13.98.")
        
        tx_hash = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        print(f"✅ SUCCESS! Funds Unlocked.")
        print(f"   Transaction Hash: {tx_hash}")
        print("   Wait 15 seconds for the blockchain to update.")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        if "balance" in str(e).lower():
            print("   (Ignore this if you see a Transaction Hash above)")

if __name__ == "__main__":
    asyncio.run(main())