import os
import asyncio
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams
from web3 import Web3

async def main():
    load_dotenv(override=True)
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS") # Get the 0x6b82... address
    host = "https://clob.polymarket.com"
    chain_id = 137

    if not proxy:
        print("❌ ERROR: POLYMARKET_PROXY_ADDRESS is missing in .env file.")
        return

    print(f"--- TARGETING PROXY: {proxy} ---")

    # 1. Web3 Balance Check (The Source of Truth)
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
    contract = w3.eth.contract(address=usdc_contract, abi=abi)
    
    raw_balance = contract.functions.balanceOf(proxy).call()
    balance_usdc = raw_balance / 1e6

    print(f"💰 REAL PROXY BALANCE: ${balance_usdc:,.2f}")

    if balance_usdc < 1:
        print("❌ ERROR: Proxy is empty. Did you send the funds to the right address?")
        return

    # 2. Authenticate & Approve
    try:
        client = ClobClient(host, key=key, chain_id=chain_id)
        try:
            creds = client.create_or_derive_api_creds()
        except:
            creds = client.create_or_derive_api_key()
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        print("✅ Authenticated")

        print("🚀 SENDING APPROVAL TRANSACTION...")
        # This tells Polymarket: "Allow trading from my Proxy"
        resp = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        print(f"✅ APPROVAL SENT! Response: {resp}")
        print("⏳ Waiting 15 seconds for blockchain confirmation...")
        await asyncio.sleep(15)
        print("✅ DONE. You should be ready to trade.")

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        if "gas" in str(e).lower():
            print("👉 CAUSE: You might need a tiny amount of MATIC for gas fees.")

if __name__ == "__main__":
    asyncio.run(main())