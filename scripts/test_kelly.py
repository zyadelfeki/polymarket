import sys
sys.path.append('.')

from decimal import Decimal
from risk.kelly_sizer import AdaptiveKellySizer

def run_kelly_sizer():
    print("\n" + "="*60)
    print("TESTING ADAPTIVE KELLY SIZER")
    print("="*60 + "\n")
    
    sizer = AdaptiveKellySizer()
    bankroll = Decimal("15.00")
    
    test_cases = [
        {"name": "High Edge (30%)", "win_prob": 0.80, "payout": 2.0, "edge": 0.30},
        {"name": "Medium Edge (15%)", "win_prob": 0.65, "payout": 1.8, "edge": 0.15},
        {"name": "Low Edge (8%)", "win_prob": 0.55, "payout": 1.5, "edge": 0.08},
        {"name": "Extreme Edge (volatility)", "win_prob": 0.90, "payout": 10.0, "edge": 0.50},
    ]
    
    print(f"Bankroll: ${bankroll}\n")
    
    for tc in test_cases:
        bet_size = sizer.calculate_bet_size(
            bankroll=bankroll,
            win_probability=tc["win_prob"],
            payout_odds=tc["payout"],
            edge=tc["edge"],
        )
        
        print(f"{tc['name']}:")
        print(f"  Win Probability: {tc['win_prob']:.0%}")
        print(f"  Payout Odds: {tc['payout']:.1f}x")
        print(f"  Edge: {tc['edge']:.1%}")
        print(f"  → Bet Size: ${bet_size:.2f} ({float(bet_size/bankroll)*100:.1f}% of bankroll)")
        print()
    
    print("Testing Streak Adjustment...")
    print()
    
    print("After 3 consecutive wins:")
    for i in range(3):
        sizer.record_trade_result(win=True, profit=1.0)
    
    bet_after_wins = sizer.calculate_bet_size(
        bankroll=Decimal("20.00"),
        win_probability=0.70,
        payout_odds=2.0,
        edge=0.20,
    )
    print(f"  Bet size increased to: ${bet_after_wins:.2f}")
    print()
    
    sizer.reset_streak()
    
    print("After 2 consecutive losses:")
    for i in range(2):
        sizer.record_trade_result(win=False, profit=-1.0)
    
    bet_after_losses = sizer.calculate_bet_size(
        bankroll=Decimal("13.00"),
        win_probability=0.70,
        payout_odds=2.0,
        edge=0.20,
    )
    print(f"  Bet size reduced to: ${bet_after_losses:.2f}")
    print()
    
    print("="*60)
    print("✅ KELLY SIZER TEST COMPLETE")
    print("="*60 + "\n")
    
    return True

if __name__ == "__main__":
    result = run_kelly_sizer()
    sys.exit(0 if result else 1)