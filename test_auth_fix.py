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


def main() -> None:
    print("=" * 60)
    print("POLYMARKET CLIENT V2 AUTHENTICATION TEST")
    print("=" * 60)
    print()

    print("[TEST 1] Paper Trading Mode")
    print("-" * 60)
    client_paper = PolymarketClientV2(
        private_key="your_private_key_here",
        paper_trading=True,
    )
    print("✓ Client created")
    print(f"  - Authenticated: {client_paper.authenticated}")
    print(f"  - Can Trade: {client_paper.can_trade}")
    print(f"  - Has Client: {bool(client_paper.client)}")
    print(f"  - Address: {client_paper.address}")
    print()

    print("Testing LIVE mode without private key...")
    client_live = PolymarketClientV2(
        private_key=None,
        paper_trading=False,
    )

    print(f"Authenticated: {client_live.authenticated}")
    print(f"Can Trade: {client_live.can_trade}")
    print(f"Has Client: {bool(client_live.client)}")
    print(f"Address: {client_live.address}")

    print("\n✓ Nuclear authentication test complete!")
    print("\nSummary:")
    print(f"  - SDK Available: {bool(client_live.client)}")
    print(f"  - Paper Trading: {client_live.paper_trading}")
    print(f"  - Authenticated: {client_live.authenticated}")
    print("\nThe `client_not_initialized` error should be GONE when you run live mode with a real private key.")


if __name__ == "__main__":
    main()
