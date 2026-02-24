"""
Diagnose why charlie_degraded_mode fires 104x but only 6 binance_features_computed.
Run from polymarket directory.
"""
import asyncio
import sys
sys.path.insert(0, "C:/Users/zyade/Downloads/project-charlie-main")

async def main():
    # 1. Test direct signal with synthetic features (no extra_features)
    print("=== Test 1: Signal with NO extra_features (synthetic) ===")
    from src.api.signals import get_signal_for_market
    sig = await get_signal_for_market("BTC", "15m")
    print(f"  p_win={sig['p_win']:.4f} confidence={sig['confidence']:.4f} regime={sig['regime']}")
    print(f"  raw_ensemble.final_signal={sig.get('raw_features', {}).get('final_signal', 'N/A')}")
    print(f"  consensus_strength={sig.get('raw_features', {}).get('consensus_strength', 'N/A')}")

    print()
    print("=== Test 2: Signal with REAL-LOOKING extra_features ===")
    real_features = {
        "rsi_14": 62.5,
        "macd": 0.0024,
        "price_vs_sma20": 0.012,
        "price_vs_sma50": 0.024,
        "volatility_20d": 0.042,
    }
    sig2 = await get_signal_for_market("BTC", "15m", extra_features=real_features)
    print(f"  p_win={sig2['p_win']:.4f} confidence={sig2['confidence']:.4f} regime={sig2['regime']}")
    print(f"  raw_ensemble.final_signal={sig2.get('raw_features', {}).get('final_signal', 'N/A')}")
    print(f"  consensus_strength={sig2.get('raw_features', {}).get('consensus_strength', 'N/A')}")

    print()
    print("=== Test 3: Charlie gate with confidence check ===")
    from integrations.charlie_booster import CharliePredictionGate
    from decimal import Decimal
    gate = CharliePredictionGate(min_edge=Decimal("0.01"), min_confidence=Decimal("0.60"))
    rec = await gate.evaluate_market(
        market_id="0xtest",
        market_price=Decimal("0.45"),
        symbol="BTC",
        timeframe="15m",
        bankroll=Decimal("1000"),
        extra_features=real_features,
    )
    print(f"  Result: {rec}")

asyncio.run(main())
