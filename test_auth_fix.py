#!/usr/bin/env python3
"""
Test script to verify forced authentication in PolymarketClientV2.
"""

import os
import sys
import structlog

# Configure structlog for clean output
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ]
)

# Import the client
from data_feeds.polymarket_client_v2 import PolymarketClientV2

print("=" * 60)
print("POLYMARKET CLIENT V2 AUTHENTICATION TEST")
print("=" * 60)
print()

# Test 1: Paper Trading Mode (no auth needed)
print("[TEST 1] Paper Trading Mode")
print("-" * 60)
client_paper = PolymarketClientV2(
    private_key="your_private_key_here",  # Fake key
    paper_trading=True
)
print(f"✓ Client created")
print(f"  - Authenticated: {client.authenticated}")
print(f"  - Can Trade: {client.can_trade}")
print(f"  - Has Client: {bool(client.client)}")
print(f"  - Address: {client.address}")
print()

# Test 2: Live mode with NO key (should fail gracefully)
print("Testing LIVE mode without private key...")
from data_feeds.polymarket_client_v2 import PolymarketClientV2

client = PolymarketClientV2(
    private_key=None,
    paper_trading=False
)

print(f"Authenticated: {client.authenticated}")
print(f"Can Trade: {client.can_trade}")
print(f"Has Client: {bool(client.client)}")
print(f"Address: {client.address}")

print("\n✓ Nuclear authentication test complete!")
print("\nSummary:")
print(f"  - SDK Available: {bool(client.client) if 'client' in dir() else 'N/A'}")
print(f"  - Paper Trading: {client.paper_trading if 'client' in dir() else 'N/A'}")
print(f"  - Authenticated: {client.authenticated if hasattr(client, 'authenticated') else 'N/A'}")
print("\nThe `client_not_initialized` error should be GONE when you run live mode with a real private key.")
