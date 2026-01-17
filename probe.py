import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

# Load env to silence warnings, but we won't use the key for auth yet
load_dotenv(override=True)
key = os.getenv("POLYMARKET_PRIVATE_KEY")
host = "https://clob.polymarket.com"
chain_id = 137

print("--- SAFE METHOD LISTING ---")
try:
    # Just initialize the object
    client = ClobClient(host, key=key, chain_id=chain_id)
    print("Client Object Created.")
    
    print("\n[AVAILABLE COMMANDS]")
    # List all public methods
    methods = [m for m in dir(client) if not m.startswith('_')]
    
    # Filter for interesting ones
    balance_methods = [m for m in methods if "balance" in m or "fund" in m or "collateral" in m or "allowance" in m]
    
    print(f"Total methods found: {len(methods)}")
    print("Potential Balance Methods:")
    for m in balance_methods:
        print(f" -> {m}")

    print("\n(Full list for debugging):")
    print(methods)

except Exception as e:
    print(f"Init Failed: {e}")