#!/usr/bin/env python3
import sys
sys.path.append('.')

import os
from pathlib import Path
from config.settings import settings

def validate_setup():
    print("\n" + "="*60)
    print("SETUP VALIDATION")
    print("="*60 + "\n")
    
    issues = []
    warnings = []
    
    print("Checking environment variables...")
    
    if not settings.POLYMARKET_PRIVATE_KEY:
        issues.append("POLYMARKET_PRIVATE_KEY not set (required for trading)")
    else:
        if len(settings.POLYMARKET_PRIVATE_KEY) < 60:
            issues.append("POLYMARKET_PRIVATE_KEY appears invalid (too short)")
        else:
            print("  ✅ POLYMARKET_PRIVATE_KEY configured")
    
    if not settings.NEWS_API_KEY:
        warnings.append("NEWS_API_KEY not set (news scanning disabled)")
    else:
        print("  ✅ NEWS_API_KEY configured")
    
    if not settings.TWITTER_BEARER_TOKEN:
        warnings.append("TWITTER_BEARER_TOKEN not set (Twitter scanning disabled)")
    else:
        print("  ✅ TWITTER_BEARER_TOKEN configured")
    
    print()
    print("Checking configuration...")
    
    if settings.INITIAL_CAPITAL < 1:
        issues.append(f"INITIAL_CAPITAL too low: ${settings.INITIAL_CAPITAL}")
    else:
        print(f"  ✅ Initial capital: ${settings.INITIAL_CAPITAL}")
    
    if settings.MAX_POSITION_SIZE_PCT > 50:
        warnings.append(f"MAX_POSITION_SIZE_PCT very high: {settings.MAX_POSITION_SIZE_PCT}%")
    else:
        print(f"  ✅ Max position size: {settings.MAX_POSITION_SIZE_PCT}%")
    
    if not settings.CIRCUIT_BREAKER_ENABLED:
        warnings.append("Circuit breaker disabled (risky)")
    else:
        print(f"  ✅ Circuit breaker enabled")
    
    print(f"  ✅ Paper trading: {settings.PAPER_TRADING}")
    
    print()
    print("Checking directories...")
    
    required_dirs = ["logs", "data", "backtest_results"]
    for dir_name in required_dirs:
        Path(dir_name).mkdir(exist_ok=True)
        print(f"  ✅ {dir_name}/ directory ready")
    
    print()
    print("Checking Python packages...")
    
    required_packages = [
        "aiohttp",
        "websockets",
        "pandas",
        "numpy",
        "vaderSentiment",
        "py_clob_client",
        "web3",
        "eth_account",
        "sqlalchemy"
    ]
    
    missing_packages = []
    for package in required_packages:
        try:
            __import__(package)
            print(f"  ✅ {package}")
        except ImportError:
            missing_packages.append(package)
            print(f"  ❌ {package} NOT INSTALLED")
    
    print()
    print("="*60)
    
    if missing_packages:
        print("❌ VALIDATION FAILED")
        print()
        print("Missing packages:")
        for pkg in missing_packages:
            print(f"  - {pkg}")
        print()
        print("Install with: pip install -r requirements.txt")
        return False
    
    if issues:
        print("❌ CRITICAL ISSUES FOUND")
        print()
        for issue in issues:
            print(f"  ❌ {issue}")
        print()
        print("Fix these issues before running the bot.")
        return False
    
    if warnings:
        print("⚠️  WARNINGS (non-critical)")
        print()
        for warning in warnings:
            print(f"  ⚠️  {warning}")
        print()
    
    print("✅ VALIDATION PASSED")
    print()
    print("Ready to run:")
    print("  1. Test components: python scripts/test_*.py")
    print("  2. Start bot: python main.py")
    print()
    print("="*60 + "\n")
    
    return True

if __name__ == "__main__":
    result = validate_setup()
    sys.exit(0 if result else 1)