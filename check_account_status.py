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

    print("--- 🕵️ ACCOUNT DETECTIVE ---")
    
    try:
        # 1. Login
        client = ClobClient(host, key=key, chain_id=chain_id)
        try:
            creds = client.create_or_derive_api_creds()
        except:
            creds = client.create_or_derive_api_key()
        
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        print("✅ Authenticated")

        # 2. Get Signer Address (The one generated from your Private Key)
        signer = client.get_address()
        print(f"🔑 SIGNER ADDRESS: {signer}")

        # 3. Ask API for Balance
        print("\n📡 ASKING POLYMARKET API FOR BALANCE...")
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        
        # Parse result
        raw_balance = resp.get('balance', '0')
        balance = float(raw_balance) / 1000000
        
        print(f"💰 API REPORTS BALANCE: ${balance:,.2f}")
        
        if balance > 1:
            print("\n✅ GREAT NEWS: The API sees your money!")
            print("   The bot was failing because it was checking the wrong 'Proxy Address'.")
            print("   We will switch the bot to trust the API instead of the config file.")
        else:
            print("\n❌ BAD NEWS: The API sees $0.00.")
            print("   This means your Private Key belongs to a DIFFERENT account than the one in your browser.")
            print("   Did you log in with a different email on the website?")

    except Exception as e:
        print(f"❌ ERROR: {e}")

if __name__ == "__main__":
    asyncio.run(main())