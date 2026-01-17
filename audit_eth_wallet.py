import os
from web3 import Web3
from dotenv import load_dotenv

def main():
    load_dotenv(override=True)
    # Target address
    address = "0x6b82023EAaEDcfE61DE197451A134809E3DD242b"
    
    print(f"--- CHECKING ETHEREUM MAINNET: {address} ---")
    
    # Connect to Ethereum Mainnet (Public RPC)
    w3 = Web3(Web3.HTTPProvider("https://eth.llamarpc.com"))
    
    if not w3.is_connected():
        print("❌ Could not connect to Ethereum RPC")
        return

    # Check ETH
    eth_bal = w3.eth.get_balance(address) / 10**18
    print(f"💎 ETH:  {eth_bal:,.4f}")

    # Check USDT (Ethereum Contract)
    usdt_contract = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
    ct = w3.eth.contract(address=usdt_contract, abi=abi)
    usdt_bal = ct.functions.balanceOf(address).call() / 10**6
    print(f"💵 USDT: ${usdt_bal:,.2f}")