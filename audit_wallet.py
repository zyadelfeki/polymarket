import os
from dotenv import load_dotenv
from web3 import Web3

def check_token(w3, contract_address, wallet_address, token_name, decimals=6):
    try:
        abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
        contract = w3.eth.contract(address=contract_address, abi=abi)
        raw = contract.functions.balanceOf(wallet_address).call()
        bal = raw / (10 ** decimals)
        if bal > 0:
            print(f"💰 FOUND: {token_name:<20} = ${bal:,.2f}")
        else:
            print(f"   Empty: {token_name:<20}")
        return bal
    except Exception as e:
        print(f"   Error checking {token_name}")
        return 0

def main():
    load_dotenv(override=True)
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS")
    
    if not proxy:
        print("Error: No Proxy Address in .env")
        return

    print(f"--- DEEP SCANNING WALLET: {proxy} ---")
    print("Network: Polygon (Mainnet)\n")
    
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    
    # 1. Check Native MATIC (POL)
    matic_raw = w3.eth.get_balance(proxy)
    matic = matic_raw / 1e18
    if matic > 0:
        print(f"💰 FOUND: MATIC (Native)       = {matic:,.4f} MATIC")
    else:
        print(f"   Empty: MATIC (Native)")

    # 2. Check Tokens
    # USDT (Tether)
    check_token(w3, "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", proxy, "USDT (Tether)", 6)
    
    # USDC.e (Bridged - What Polymarket WANTS)
    check_token(w3, "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", proxy, "USDC.e (Bridged)", 6)
    
    # USDC (Native - What you might have sent)
    check_token(w3, "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", proxy, "USDC (Native)", 6)
    
    # WETH
    check_token(w3, "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619", proxy, "WETH (Wrapped Eth)", 18)

if __name__ == "__main__":
    main()