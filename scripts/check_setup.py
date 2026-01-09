#!/usr/bin/env python3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
import os

def check_setup():
    print("\n" + "="*60)
    print("🔍 POLYMARKET BOT SETUP CHECK")
    print("="*60 + "\n")
    
    checks = [
        ("Python Version", sys.version_info >= (3, 11), f"Python {sys.version_info.major}.{sys.version_info.minor}"),
        (".env file exists", Path(".env").exists(), "Found" if Path(".env").exists() else "Missing"),
        ("NewsAPI Key", bool(settings.NEWS_API_KEY), "Configured" if settings.NEWS_API_KEY else "Missing"),
        ("Polymarket API Key", bool(settings.POLYMARKET_API_KEY), "Configured" if settings.POLYMARKET_API_KEY else "Missing"),
        ("Database directory", Path("data").exists(), "Exists" if Path("data").exists() else "Will be created"),
        ("Logs directory", Path("logs").exists(), "Exists" if Path("logs").exists() else "Will be created")
    ]
    
    all_passed = True
    for name, passed, detail in checks:
        status = "✅" if passed else "❌"
        print(f"{status} {name}: {detail}")
        if not passed and name in ["NewsAPI Key", "Polymarket API Key"]:
            all_passed = False
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ SETUP COMPLETE - Ready to run!")
        print("\nStart bot with: python main.py")
    else:
        print("⚠️  SETUP INCOMPLETE")
        print("\nNext steps:")
        print("1. Copy .env.example to .env")
        print("2. Add your API keys to .env")
        print("3. Run: python scripts/test_feeds.py")
    print("="*60 + "\n")
    
    return all_passed

if __name__ == "__main__":
    passed = check_setup()
    sys.exit(0 if passed else 1)