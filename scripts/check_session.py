"""Session health check — run after a paper session to verify all fixes."""
import json
from collections import Counter
import sys

log_path = sys.argv[1] if len(sys.argv) > 1 else "logs/production.log"

counts = Counter()
try:
    with open(log_path) as f:
        for line in f:
            try:
                counts[json.loads(line).get("event", "")] += 1
            except Exception:
                pass
except FileNotFoundError:
    print(f"ERROR: {log_path} not found — did the bot run with LOG_FORMAT=json?")
    sys.exit(1)

scans = max(counts["strategy_scan_begin"], 1)
total = sum(counts.values())
print(f"=== SESSION STATS ===")
print(f"  Total log lines  : {total}")
print(f"  strategy_scan_begin: {scans}")
print()

print("--- FIXED (must be 0) ---")
fixed = [
    ("periodic_check_failed",     "was 292"),
    ("performance_halt_win_rate",  "was 222 trips in paper"),
    ("circuit_breaker_attribute_error", "was present"),
]
all_fixed = True
for ev, note in fixed:
    c = counts[ev]
    status = "OK" if c == 0 else f"STILL FIRING"
    flag = "" if c == 0 else " <-- CHECK"
    print(f"  {status:12s}  {ev}: {c}  ({note}){flag}")
    if c > 0:
        all_fixed = False

print()
print("--- DEGRADED MODE RATIO (target: <3x) ---")
deg  = counts["charlie_degraded_mode"]
comp = max(counts["binance_features_computed"], 1)
ratio = deg / comp
ratio_flag = "" if ratio < 3 else " <-- still high"
print(f"  charlie_degraded_mode     : {deg}")
print(f"  binance_features_computed : {comp}")
print(f"  ratio                     : {ratio:.1f}x  (was 30x){ratio_flag}")

print()
print("--- SIGNALS FLOWING (must all be > 0) ---")
for ev in ["strategy_scan_begin", "charlie_gate_approved", "order_submitted", "binance_features_computed"]:
    c = counts[ev]
    flag = "" if c > 0 else " <-- MISSING"
    print(f"  {ev}: {c}{flag}")

print()
print("--- REJECTION BREAKDOWN ---")
for ev in [
    "charlie_gate_rejected",
    "charlie_coin_flip_rejected",
    "kelly_size_zero",
    "order_blocked_global_risk_budget_exceeded",
    "order_blocked_per_market_budget_exceeded",
    "order_blocked_portfolio_risk",
    "opportunity_skipped",
    "paper_order_cooldown",
]:
    if counts[ev]:
        print(f"  {ev}: {counts[ev]}")

print()
if all_fixed and ratio < 3:
    print("=== ALL CHECKS PASSED — run the Kelly sweep next ===")
    print("  python experiments/sweep_kelly_and_edge.py --log logs/production.log")
else:
    print("=== SOME ISSUES REMAIN — see flags above ===")
