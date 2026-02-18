import re
from pathlib import Path


log_file = Path("bot_production.log")
log_content = log_file.read_text(encoding="utf-8", errors="ignore")

print("=" * 60)
print("PHASE 6 LIVE TEST ANALYSIS")
print("=" * 60)

# 1. Growth mode activation
growth_mode = re.findall(r"growth_mode_active.*?bankroll[=:].*?([\d.]+)", log_content)
print(f"\n🎯 Growth Mode Activations: {len(growth_mode)}")
if growth_mode:
    print(f"   Latest Bankroll: ${growth_mode[-1]}")

# 2. Position round-ups
roundups = re.findall(
    r"position_rounded_up.*?kelly_suggested[=:].*?([\d.]+).*?executed[=:].*?([\d.]+)",
    log_content,
)
print(f"\n📈 Positions Rounded Up: {len(roundups)}")
if roundups:
    for kelly, executed in roundups[-3:]:
        print(f"   Kelly: ${kelly} → Executed: ${executed}")

# 3. Balance syncs
balance_syncs = re.findall(r"balance_synced.*?current[=:].*?([\d.]+)", log_content)
print(f"\n💰 Balance Syncs: {len(balance_syncs)}")
if len(balance_syncs) >= 2:
    start_balance = float(balance_syncs[0])
    end_balance = float(balance_syncs[-1])
    change = end_balance - start_balance
    print(f"   Start: ${start_balance:.2f}")
    print(f"   End: ${end_balance:.2f}")
    print(f"   Change: ${change:+.2f} ({(change / start_balance) * 100:+.1f}%)")

# 4. Market detection (Bitcoin vs BTC)
bitcoin_markets = len(re.findall(r"Bitcoin.*?market.*?found|detected", log_content, re.I))
btc_markets = len(re.findall(r"\bBTC\b.*?market.*?found|detected", log_content))
print("\n🔍 Market Detection:")
print(f"   'Bitcoin' markets: {bitcoin_markets}")
print(f"   'BTC' markets: {btc_markets}")
print(f"   Total: {bitcoin_markets + btc_markets}")

# 5. Fee adjustments
fee_adjustments = re.findall(
    r"edge_adjusted_for_fees.*?raw_edge[=:].*?([\d.]+).*?net_edge[=:].*?([\d.]+)",
    log_content,
)
print(f"\n💸 Fee-Adjusted Edges: {len(fee_adjustments)}")
if fee_adjustments:
    for raw, net in fee_adjustments[-3:]:
        print(f"   Raw: {float(raw) * 100:.1f}% → Net: {float(net) * 100:.1f}%")

# 6. Opportunities vs Executions
opps = len(re.findall(r"opportunity_found", log_content))
executed = len(re.findall(r"order_executed|paper_trade_signal", log_content))
print("\n📊 EXECUTION METRICS:")
print(f"   Opportunities: {opps}")
print(f"   Executed: {executed}")
if opps > 0:
    rate = (executed / opps) * 100
    print(f"   Execution Rate: {rate:.1f}%")

    if rate == 0:
        print("   ❌ STILL NOT TRADING")
    elif rate < 30:
        print("   ⚠️  LOW (needs investigation)")
    elif rate < 50:
        print("   ✅ ACCEPTABLE")
    else:
        print("   🎉 EXCELLENT!")

# 7. Errors
errors = len(re.findall(r"ERROR|CRITICAL|Traceback", log_content))
print(f"\n🚨 Errors: {errors}")

print("\n" + "=" * 60)