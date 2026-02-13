#!/usr/bin/env python3
"""Quick performance check - run anytime to see bot stats."""

import json
from pathlib import Path
from decimal import Decimal

metrics_path = Path('data/metrics.jsonl')

if not metrics_path.exists():
    print("❌ No metrics file. Is the bot running?")
    exit(1)

with open(metrics_path) as f:
    lines = f.readlines()

if not lines:
    print("❌ Metrics file is empty.")
    exit(1)

latest = json.loads(lines[-1])

print("\n" + "="*60)
print("📊 TRADING BOT PERFORMANCE")
print("="*60)
print(f"\n⏰ Last Update: {latest['timestamp']}")
print(f"💰 Equity: ${latest['equity']:.2f}")
print(f"📈 Trades: {latest['trades_executed']}")
print(f"🎯 Win Rate: {latest['win_rate'] * 100:.1f}%")
print(f"📊 Sharpe: {latest['sharpe_ratio']:.2f}")
print(f"📉 Max DD: {latest['max_drawdown_pct']:.1f}%")
print(f"⚡ Latency: {latest['avg_execution_ms']:.1f}ms")
print(f"🔔 Signals: {latest['signals_generated']}")
print(f"📦 Open: {latest['open_positions']}")
print(f"⚙️  CB State: {latest['circuit_breaker']}")

# Hourly stats
if len(lines) >= 12:
    recent = [json.loads(line) for line in lines[-12:]]
    equity_change = recent[-1]['equity'] - recent[0]['equity']
    trades_last_hour = recent[-1]['trades_executed'] - recent[0]['trades_executed']
    
    print(f"\n📊 Last Hour:")
    print(f"   Equity: ${equity_change:+.2f}")
    print(f"   Trades: {trades_last_hour}")

# Success criteria
print("\n" + "="*60)
print("✅ SUCCESS CRITERIA")
print("="*60)

equity = latest['equity']
trades = latest['trades_executed']
win_rate = latest['win_rate'] * 100
max_dd = latest['max_drawdown_pct']
cb = latest['circuit_breaker']

checks = [
    (equity > 9500, f"Equity > $9,500: {'✅' if equity > 9500 else '❌'} (${equity:.2f})"),
    (cb == 'CLOSED', f"CB Not Tripped: {'✅' if cb == 'CLOSED' else '❌'} ({cb})"),
    (trades >= 1, f"Has Traded: {'✅' if trades >= 1 else '❌'} ({trades} trades)"),
    (win_rate >= 50 or trades < 5, f"Win Rate ≥50%: {'✅' if win_rate >= 50 or trades < 5 else '❌'} ({win_rate:.1f}%)"),
    (max_dd < 5, f"Max DD < 5%: {'✅' if max_dd < 5 else '❌'} ({max_dd:.1f}%)"),
]

for check, msg in checks:
    print(msg)

all_good = all(c[0] for c in checks)
if all_good:
    print("\n🎉 READY FOR LIVE TRADING!")
else:
    print("\n⚠️  Keep monitoring...")

print("\n" + "="*60 + "\n")
