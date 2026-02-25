"""
Diagnose Charlie signal quality.

Tests:
  1. Synthetic (no extra_features) — must return HOLD / p_win=0.5
  2. Current live BTC features — must return BUY/SELL with p_win != 0.5
  3. End-to-end gate check — must return a TradeRecommendation

Run from polymarket directory:
  python scripts/diagnose_charlie.py
"""
import asyncio
import sys
sys.path.insert(0, "C:/Users/zyade/polymarket")
sys.path.insert(0, "C:/Users/zyade/Downloads/project-charlie-main")


async def main():
    from src.api.signals import get_signal_for_market

    # ------------------------------------------------------------------ Test 1
    print("=== Test 1: No extra_features (synthetic neutral — expect HOLD) ===")
    sig = await get_signal_for_market("BTC", "15m")
    _ensemble = sig.get("raw_features", {}).get("ensemble", {})
    print(f"  p_win={sig['p_win']:.4f}  confidence={sig['confidence']:.4f}  regime={sig['regime']}")
    print(f"  ensemble.final_signal={_ensemble.get('final_signal', 'N/A')}")
    print(f"  ensemble.consensus_strength={_ensemble.get('consensus_strength', 'N/A')}")
    print(f"  coin_flip_blocked: {abs(sig['p_win'] - 0.5) < 0.03}")

    # ------------------------------------------------------------------ Test 2
    print()
    print("=== Test 2: Current live BTC features (expect BUY, p_win>0.53) ===")
    # Values from a recent Binance fetch — volatile_20d is often <0.02 in quiet sessions
    live_features = {
        "rsi_14": 51.78,
        "macd": 0.521,
        "price_vs_sma20": -0.000589,
        "price_vs_sma50": 0.003363,
        "volatility_20d": 0.001934,
        "book_imbalance": 0.3771,
    }
    sig2 = await get_signal_for_market("BTC", "15m", extra_features=live_features)
    _ens2 = sig2.get("raw_features", {}).get("ensemble", {})
    print(f"  p_win={sig2['p_win']:.4f}  confidence={sig2['confidence']:.4f}  regime={sig2['regime']}")
    print(f"  ensemble.final_signal={_ens2.get('final_signal', 'N/A')}")
    print(f"  ensemble.model_votes={_ens2.get('model_votes', 'N/A')}")
    print(f"  coin_flip_blocked: {abs(sig2['p_win'] - 0.5) < 0.03}")
    if abs(sig2["p_win"] - 0.5) >= 0.03:
        print("  CONFIRMED: Charlie produces directional signal with live features")
    else:
        print("  STILL BLOCKED: check model thresholds vs current market conditions")

    # ------------------------------------------------------------------ Test 3
    print()
    print("=== Test 3: End-to-end charlie_gate (market_price=0.45, expect approval) ===")
    from integrations.charlie_booster import CharliePredictionGate
    from decimal import Decimal

    gate = CharliePredictionGate(min_edge=Decimal("0.01"), min_confidence=Decimal("0.60"))
    rec = await gate.evaluate_market(
        market_id="0xtest",
        market_price=Decimal("0.45"),
        symbol="BTC",
        timeframe="15m",
        bankroll=Decimal("1000"),
        extra_features=live_features,
    )
    if rec is not None:
        print(f"  APPROVED: side={rec.side}  p_win={rec.p_win:.4f}  edge={rec.edge:.4f}  size=${rec.size:.2f}")
    else:
        print("  REJECTED: gate returned None — check edge/confidence filters")


asyncio.run(main())

