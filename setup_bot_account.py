import os
import time
from dotenv import load_dotenv
from web3 import Web3

def get_token_balance(w3, contract_address, wallet, decimals=6):
    try:
        abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
        contract = w3.eth.contract(address=contract_address, abi=abi)
        return contract.functions.balanceOf(wallet).call() / (10**decimals)
    except:
        return 0

def main():
    load_dotenv(override=True)
    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    
    # ADDRESSES
    signer = "0x4C71275D76334FADfdA7A232933bbe323625C980"
    proxy  = "0x6b82023EAaEDcfE61DE197451A134809E3DD242b"
    
    print("--- 🕵️ ULTIMATE WALLET SCANNER ---")
    print(f"   Target: {signer}")

    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    if not w3.is_connected():
        print("❌ Connect Error")
        return

    # 1. Check Gas
    matic_bal = w3.eth.get_balance(signer) / 10**18
    print(f"\n⛽ MATIC (Gas): {matic_bal:.4f}")
    if matic_bal < 0.05:
        print("   ❌ CRITICAL: You need at least 0.1 MATIC to move funds.")
        print(f"   👉 Send 1 MATIC to: {signer}")

    # 2. Check USDCs
    BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    NATIVE  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    
    s_bridged = get_token_balance(w3, BRIDGED, signer)
    s_native  = get_token_balance(w3, NATIVE, signer)
    p_bridged = get_token_balance(w3, BRIDGED, proxy)
    
    print(f"\n💰 SIGNER WALLET (Entry Point):")
    print(f"   Bridged USDC: ${s_bridged:,.2f}")
    print(f"   Native USDC:  ${s_native:,.2f}")

    print(f"\n🏦 PROXY WALLET (Trading Account):")
    print(f"   Bridged USDC: ${p_bridged:,.2f}")

    # 3. Logic
    total_signer = s_bridged + s_native
    if p_bridged > 1:
        print("\n✅ FUNDS FOUND IN PROXY! You are ready to trade.")
        print(f"   Update .env -> POLYMARKET_PROXY_ADDRESS={proxy}")
    elif total_signer > 1:
        print("\n⚠️  FUNDS FOUND IN SIGNER.")
        if matic_bal > 0.01:
            print("   ✅ You have Gas. We can move this now.")
            # (Transfer logic would go here, but let's find the money first)
        else:
            print("   ❌ You have money but NO GAS.")
            print("   You MUST send MATIC to this address to unlock it.")
    else:
        print("\n❌ NO FUNDS FOUND YET.")
        print("   The withdrawal might still be pending on the blockchain.")
        print(f"   Check here: https://polygonscan.com/address/{signer}")

if __name__ == "__main__":
    main()