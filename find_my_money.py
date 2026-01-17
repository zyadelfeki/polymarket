import os
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient

def check_balance(w3, token_address, wallet_address, token_name):
    abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
    contract = w3.eth.contract(address=token_address, abi=abi)
    raw = contract.functions.balanceOf(wallet_address).call()
    bal = raw / 1e6
    print(f"   {token_name}: ${bal:,.2f}")
    return bal

def main():
    load_dotenv(override=True)
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS")
    host = "https://clob.polymarket.com"
    chain_id = 137

    # Connect to Polygon
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    
    # 1. Derive Signer Address
    client = ClobClient(host, key=key, chain_id=chain_id)
    signer = client.get_address()
    
    print(f"🔎 SCANNING FOR FUNDS...")
    print(f"   Signer (EOA): {signer}")
    print(f"   Proxy (Safe): {proxy}")
    print("-" * 40)

    # Contract Addresses
    BRIDGED_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" # Polymarket uses this
    NATIVE_USDC  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359" # "New" USDC

    print("\n[1] CHECKING PROXY WALLET (0x6b8...)")
    b1 = check_balance(w3, BRIDGED_USDC, proxy, "Bridged USDC (USDC.e)")
    b2 = check_balance(w3, NATIVE_USDC,  proxy, "Native USDC")

    print("\n[2] CHECKING SIGNER WALLET (0x4C7...)")
    b3 = check_balance(w3, BRIDGED_USDC, signer, "Bridged USDC (USDC.e)")
    b4 = check_balance(w3, NATIVE_USDC,  signer, "Native USDC")

    print("\n" + "="*40)
    print("VERDICT:")
    if b1 > 1:
        print("✅ SUCCESS! Funds are in Proxy as Bridged USDC.")
        print("   Run 'approve_funds.py' again (it should work now).")
    elif b2 > 1:
        print("⚠️  FOUND IN NATIVE USDC (Proxy).")
        print("   Action: Go to Polymarket.com -> Portfolio.")
        print("   Look for an 'Activate Funds' banner to swap Native -> Bridged.")
    elif b3 > 1 or b4 > 1:
        print("⚠️  FOUND IN SIGNER WALLET.")
        print(f"   Action: Send funds from {signer} -> {proxy}")
    else:
        print("❌ FUNDS NOT FOUND.")
        print("   Did you send USDT? Or to a different address?")

if __name__ == "__main__":
    main()