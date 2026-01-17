import os
from dotenv import load_dotenv

print("--- DEBUGGING ENV ---")
# Force load the .env file
loaded = load_dotenv(verbose=True)
print(f"File Loaded: {loaded}")

# Check the keys (masked)
key = os.getenv("POLYMARKET_PRIVATE_KEY")
address = os.getenv("POLYMARKET_PROXY_ADDRESS")

if key:
    print(f"Private Key Found: YES (Length: {len(key)})")
    print(f"Starts with: {key[:4]}...")
else:
    print("Private Key Found: NO")

if address:
    print(f"Proxy Address Found: YES ({address})")
else:
    print("Proxy Address Found: NO")
print("--- END DEBUG ---")