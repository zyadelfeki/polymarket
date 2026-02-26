#!/usr/bin/env python3
"""
Session health diagnostic.
Usage: python scripts/check_session.py [path/to/production.log]
       (defaults to logs/production.log)
"""
import sys
import json
from pathlib import Path
from collections import Counter

log_path = Path(sys.argv[1] if len(sys.argv) > 1 else "logs/production.log")
if not log_path.exists():
    print(f"ERROR: log not found: {log_path}")
    sys.exit(1)

lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
events = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
        if isinstance(obj, dict):   # skip plain strings like "HTTP/2 200 OK"
            events.append(obj)
    except json.JSONDecodeError:
        pass


def count(event_name):
    return sum(1 for e in events if e.get("event") == event_name)


def count_field(event_name, field, value):
    return sum(1 for e in events
               if e.get("event") == event_name and e.get(field) == value)


# ---------- core counters ----------
total_lines       = len(lines)
scans             = count("strategy_scan_begin")
features          = count("binance_features_computed")
charlie_approved  = count("charlie_gate_approved")
opp_detected      = count("arbitrage_opportunity_detected")
order_submitted   = count("order_submitted")
order_settled     = count("order_settled")

# Previously-fixed events (should stay at 0)
periodic_failed   = count("periodic_check_failed")
cb_attr_err       = count("circuit_breaker_attribute_error")

# performance_halt_win_rate: CRITICAL in live mode (trips CB), WARNING in paper mode.
# Count live-mode (actionable) halts only — paper-mode fires are log_only artefacts.
perf_halt_win_rate_live = sum(
    1 for e in events
    if e.get("event") == "performance_halt_win_rate" and e.get("action") != "log_only"
)
perf_halt_win_rate_paper = sum(
    1 for e in events
    if e.get("event") == "performance_halt_win_rate" and e.get("action") == "log_only"
)

# performance_halt_drawdown: CRITICAL in live mode (trips CB), WARNING in paper mode.
# Count live-mode (actionable) halts only — paper-mode fires are cross-session artefact.
perf_halt_drawdown_live = sum(
    1 for e in events
    if e.get("event") == "performance_halt_drawdown" and e.get("action") != "log_only"
)
perf_halt_drawdown_paper = sum(
    1 for e in events
    if e.get("event") == "performance_halt_drawdown" and e.get("action") == "log_only"
)

# Degraded mode
degraded_mode     = count("charlie_degraded_mode")
ratio_str = (
    f"{degraded_mode / features:.1f}x  (target: <3x)"
    if features > 0
    else "N/A (0 features computed)"
)

# Rejection reasons
rejection_events = [
    "charlie_gate_rejected",
    "charlie_gate_exception",
    "charlie_coin_flip_rejected",
    "order_blocked_no_charlie_signal",
    "order_blocked_global_risk_budget_exceeded",
    "order_blocked_per_market_budget_exceeded",
    "order_blocked_kelly_size_too_small",
]
opportunity_skip_reasons = Counter(
    e.get("reason", "unknown")
    for e in events
    if e.get("event") == "opportunity_skipped"
)
# risk_rejected = circuit breaker blocked (fires BEFORE Charlie is called)
risk_rejected_reasons = Counter(
    e.get("reason", "unknown")
    for e in events
    if e.get("event") == "risk_rejected"
)
# order_blocked_portfolio_risk = portfolio risk engine cap (fires AFTER Charlie)
portfolio_blocked_reasons = Counter(
    e.get("reason", "unknown")
    for e in events
    if e.get("event") == "order_blocked_portfolio_risk"
)

# Circuit breaker state history events
cb_trip_events     = [e for e in events if e.get("event") == "circuit_breaker_tripped"]
cb_half_open_events = [e for e in events if e.get("event") == "circuit_breaker_half_open"]
cb_recovered_events = [e for e in events if e.get("event") == "circuit_breaker_recovered"]
cb_init_events      = [e for e in events if e.get("event") == "circuit_breaker_initialized"]

print(f"\n=== SESSION STATS ===")
print(f"  Total log lines  : {total_lines}")
print(f"  strategy_scan_begin: {scans}")

print(f"\n--- FIXED (must be 0) ---")
ok_fixed = True
for name, val in [
    ("periodic_check_failed",                    periodic_failed),
    ("performance_halt_win_rate [live-mode]",     perf_halt_win_rate_live),
    ("circuit_breaker_attribute_error",           cb_attr_err),
    ("performance_halt_drawdown [live-mode]",     perf_halt_drawdown_live),
]:
    tag = "OK  " if val == 0 else "FAIL"
    if val != 0:
        ok_fixed = False
    print(f"  {tag}          {name}: {val}")
if perf_halt_win_rate_paper > 0:
    print(f"  INFO          performance_halt_win_rate [paper log_only]: {perf_halt_win_rate_paper}"
          f"  (paper-mode win-rate warning \u2014 CB not tripped, safe to ignore)")
if perf_halt_drawdown_paper > 0:
    print(f"  INFO          performance_halt_drawdown [paper log_only]: {perf_halt_drawdown_paper}"
          f"  (cross-session historical drawdown \u2014 CB not tripped, safe to ignore)")

print(f"\n--- DEGRADED MODE RATIO (target: <3x) ---")
print(f"  charlie_degraded_mode     : {degraded_mode}")
print(f"  binance_features_computed : {features}")
print(f"  ratio                     : {ratio_str}")

print(f"\n--- SIGNALS FLOWING (must all be > 0) ---")
signals_ok = True
signal_checks = [
    ("strategy_scan_begin",            scans),
    ("arbitrage_opportunity_detected", opp_detected),
    ("charlie_gate_approved",          charlie_approved),
    ("order_submitted",                order_submitted),
]
for name, val in signal_checks:
    tag = ""
    if val == 0:
        tag = "  <-- MISSING"
        signals_ok = False
    print(f"  {name}: {val}{tag}")
print(f"  order_settled: {order_settled}  (needs market resolution — OK if 0 short-term)")
print(f"  binance_features_computed: {features}")

print(f"\n--- CIRCUIT BREAKER HISTORY ---")
if cb_init_events:
    e0 = cb_init_events[0]
    print(f"  initialized: equity={e0.get('initial_equity')}, "
          f"max_drawdown={e0.get('max_drawdown_pct')}%, "
          f"daily_loss={e0.get('daily_loss_limit_pct')}%")
if cb_trip_events:
    for e in cb_trip_events:
        print(f"  TRIPPED: reason={e.get('reason')}  equity={e.get('equity')}  "
              f"drawdown={e.get('drawdown_pct', 0):.1f}%  "
              f"losses={e.get('consecutive_losses')}")
else:
    print("  (no trips this session)")
if cb_half_open_events:
    print(f"  half_open transitions: {len(cb_half_open_events)}")
if cb_recovered_events:
    print(f"  full recoveries: {len(cb_recovered_events)}")

# risk_rejected events include cb_state/half_open_max_pct since latest fix
if risk_rejected_reasons:
    first_rr = next((e for e in events if e.get("event") == "risk_rejected"), None)
    if first_rr and first_rr.get("cb_state"):
        print(f"  last known CB state when blocking: state={first_rr.get('cb_state')}  "
              f"half_open_max={first_rr.get('half_open_max_pct')}%  "
              f"peak_equity={first_rr.get('peak_equity')}")

print(f"\n--- REJECTION BREAKDOWN ---")
found_any_rejection = False

# 1. Circuit breaker (fires BEFORE Charlie — if non-zero, Charlie was never called)
total_rr = sum(risk_rejected_reasons.values())
if total_rr > 0:
    found_any_rejection = True
    print(f"  risk_rejected (circuit_breaker): {total_rr}  <-- fires BEFORE Charlie")
    for reason, c in risk_rejected_reasons.most_common():
        print(f"    └─ {reason}: {c}")

# 2. Per-event rejection items (Charlie, global/per-market budget, etc.)
for ev in rejection_events:
    c = count(ev)
    if c > 0:
        found_any_rejection = True
        if ev == "charlie_gate_rejected":
            reasons = Counter(
                e.get("reason", "unknown")
                for e in events
                if e.get("event") == ev
            )
            p_wins = [
                e.get("p_win")
                for e in events
                if e.get("event") == ev and e.get("p_win") is not None
            ]
            p_win_summary = ""
            if p_wins:
                p_win_summary = f"  (p_win range: {min(p_wins):.3f}–{max(p_wins):.3f})"
            print(f"  {ev}: {c}{p_win_summary}")
            for reason, rc in reasons.most_common():
                print(f"    └─ reason={reason}: {rc}")
        else:
            print(f"  {ev}: {c}")

# 3. Portfolio risk engine breakdown (fires AFTER circuit breaker + Charlie)
if portfolio_blocked_reasons:
    found_any_rejection = True
    total_pb = sum(portfolio_blocked_reasons.values())
    print(f"  order_blocked_portfolio_risk: {total_pb}")
    for reason, c in portfolio_blocked_reasons.most_common():
        print(f"    └─ {reason}: {c}")

if opportunity_skip_reasons:
    found_any_rejection = True
    for reason, c in opportunity_skip_reasons.most_common():
        print(f"  opportunity_skipped[{reason}]: {c}")

if not found_any_rejection:
    print("  (none — if charlie_gate_approved=0 check for charlie_gate_rejected events)")

# ---------- final verdict ----------
all_ok = ok_fixed and signals_ok
print()
if all_ok:
    print("=== ALL CHECKS PASSED — run the Kelly sweep next ===")
    print("  python experiments/sweep_kelly_and_edge.py --log logs/production.log")
else:
    print("=== CHECKS FAILED — DO NOT RUN SWEEP YET ===")
    if not ok_fixed:
        print("  Previously-fixed errors have returned.")
    if not signals_ok:
        total_circuit_blocked = sum(risk_rejected_reasons.values())
        if opp_detected == 0:
            print("  No opportunities detected. Check strategy engine and min_edge config.")
        elif total_circuit_blocked > 0 and charlie_approved == 0:
            print("  Circuit breaker blocked all opportunities BEFORE Charlie was called.")
            for reason, c in risk_rejected_reasons.most_common():
                print(f"    \u2514\u2500 {reason}: {c}")
            print("  Check: risk/system_circuit_breaker.py and daily_loss / drawdown state in DB.")
            print("  If circuit_breaker_blocked: the CB may have tripped from prior paper losses.")
        elif charlie_approved == 0:
            coin_flip = count("charlie_coin_flip_rejected")
            if coin_flip > 0 and features < opp_detected // 3:
                print("  charlie_coin_flip_rejected fired — Binance features not reaching Charlie.")
                print(f"  binance_features_computed={features}  vs  opportunities={opp_detected}")
                print("  Likely cause: api.binance.com unreachable or throttled.")
                print("  Fix: check logs for 'binance_candle_fetch_failed'.  Re-run after network check.")
                print("  Run: python scripts/diagnose_charlie.py to test signal pipeline directly.")
            elif coin_flip > 0:
                print("  charlie_coin_flip_rejected: features arriving but models return HOLD.")
                print("  Run: python scripts/diagnose_charlie.py to inspect per-model votes.")
                print("  Check: prediction_engine.py SVM rsi threshold vs current live RSI value.")
            else:
                print("  Opportunities detected but Charlie approved NONE.")
                print("  Next step: search logs for 'charlie_gate_rejected' events.")
                print("  Check the p_win range and reason breakdown above.")
        elif order_submitted == 0:
            print("  Charlie approved but orders not submitted. Check execution service.")

# ---------- calibration progress ----------
print(f"\n--- CALIBRATION PROGRESS ---")
_cal_csv = Path("data/calibration_dataset.csv")
if _cal_csv.exists():
    import csv as _csv
    _cal_p, _cal_a = [], []
    with open(_cal_csv, "r", newline="") as _cf:
        for _row in _csv.DictReader(_cf):
            try:
                _cal_p.append(float(_row["p_win_raw"]))
                _cal_a.append(int(_row["actual_outcome"]))
            except (KeyError, ValueError):
                pass
    _cal_n = len(_cal_p)
    print(f"  Calibration samples: {_cal_n}")
    if _cal_n > 0:
        # ECE (10-bin)
        _n_bins = 10
        _bins = [[] for _ in range(_n_bins)]
        for _p, _a in zip(_cal_p, _cal_a):
            _idx = min(int(_p * _n_bins), _n_bins - 1)
            _bins[_idx].append((_p, _a))
        _ece = 0.0
        for _b in _bins:
            if not _b:
                continue
            _mc = sum(x[0] for x in _b) / len(_b)
            _ma = sum(x[1] for x in _b) / len(_b)
            _ece += (len(_b) / _cal_n) * abs(_mc - _ma)
        _avg_p = sum(_cal_p) / _cal_n
        _win_rate = sum(_cal_a) / _cal_n
        print(f"  Uncalibrated ECE: {_ece:.4f}")
        print(f"  Avg p_win_raw: {_avg_p:.4f}  |  Actual win rate: {_win_rate:.4f}")
    if _cal_n >= 100:
        print(f"  ✓ CALIBRATION READY — run: python scripts/fit_calibration.py")
    else:
        _remaining = 100 - _cal_n
        print(f"  ⏳ Need {_remaining} more samples before fitting (target: 100)")
        print(f"     At ~20-30 settlements/day → ~{max(1, _remaining // 25)} more days")
else:
    print("  (no calibration data yet — run: python scripts/build_calibration_dataset.py)")

# ---------- PnL attribution by market ----------
print(f"\n--- PnL ATTRIBUTION BY MARKET ---")
import asyncio as _asyncio
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

async def _print_pnl_by_market():
    try:
        from database.ledger_async import AsyncLedger
        ledger = AsyncLedger()
        await ledger.pool.initialize()
        rows = await ledger.get_pnl_by_market()
        await ledger.close()

        if not rows:
            print("  (no settled orders yet)")
            return

        hdr = f"  {'market_id':>12}  {'trades':>6}  {'wins':>5}  {'win%':>6}  {'total_pnl':>10}  {'avg_edge':>9}  {'avg_p_win':>9}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in rows:
            win_pct = f"{100*r['win_count']/r['trade_count']:.0f}%" if r["trade_count"] > 0 else " N/A"
            avg_e   = f"{r['avg_edge']:.4f}" if r["avg_edge"] is not None else "  N/A"
            avg_pw  = f"{r['avg_p_win']:.4f}" if r["avg_p_win"] is not None else "  N/A"
            print(
                f"  {str(r['market_id']):>12}  {r['trade_count']:>6}  {r['win_count']:>5}"
                f"  {win_pct:>6}  {float(r['total_pnl']):>+10.4f}  {avg_e:>9}  {avg_pw:>9}"
            )
    except Exception as _e:
        print(f"  (pnl_by_market unavailable: {_e})")

_asyncio.run(_print_pnl_by_market())

