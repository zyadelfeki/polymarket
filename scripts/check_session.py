#!/usr/bin/env python3
"""
Session health diagnostic.
Usage: python scripts/check_session.py [path/to/production.log]
       (defaults to logs/production.log)
"""
import sys
import json
import sqlite3
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
        if isinstance(obj, dict):
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
opps_evaluated    = count("opportunity_evaluated") or opp_detected  # fallback
order_submitted   = count("order_submitted")
order_settled     = count("order_settled")

# ERROR order diagnostics — direct DB read so this captures all sessions.
_error_orders_total = 0
_error_orders_by_market: dict = {}
try:
    _db = sqlite3.connect("data/trading.db")
    _total_row = _db.execute(
        "SELECT COUNT(*) FROM order_tracking WHERE order_state = 'ERROR'"
    ).fetchone()
    _error_orders_total = _total_row[0] if _total_row else 0
    _by_market = _db.execute(
        "SELECT market_id, notes, COUNT(*) AS n "
        "FROM order_tracking WHERE order_state = 'ERROR' "
        "GROUP BY market_id, notes ORDER BY n DESC LIMIT 10"
    ).fetchall()
    for mid, note, n in _by_market:
        key = f"{mid} [{note}]"
        _error_orders_by_market[key] = n
    _db.close()
except Exception as _dbe:
    _error_orders_by_market[f"db_error: {_dbe}"] = 0

# market_blocked events from this session
market_blocked_static    = count_field("market_blocked", "reason", "static_blocked_markets")
market_blocked_perf      = count_field("market_blocked", "reason", "performance_guard_auto_block")
perf_guard_checked       = count("performance_guard_checked")
market_auto_blocked      = count("market_auto_blocked_performance")
order_error_transitions  = count("order_state_set_to_error")

# OFI signal events
ofi_signal_confirmed     = count("ofi_signal_confirmed")
ofi_conflict             = count("ofi_conflict")
# OFI feed degradation (Tier 2 data contract guard)
ofi_feed_degraded        = count("ofi_feed_degraded")

# ---------- Session 1-4: ML feature gates ----------
meta_gate_approved     = count("meta_gate_approved")
meta_gate_rejected     = count("meta_gate_rejected")
meta_gate_dec_approved = count_field("meta_gate_decision", "decision", "approved")
meta_gate_dec_rejected = count_field("meta_gate_decision", "decision", "rejected")
_meta_gate_probas = [
    e.get("proba") for e in events
    if e.get("event") == "meta_gate_decision" and e.get("proba") is not None
]
_meta_gate_avg_proba = (
    round(sum(_meta_gate_probas) / len(_meta_gate_probas), 3)
    if _meta_gate_probas else None
)
market_blocked_tag     = count("market_blocked_tag")
regime_size_adj        = count("regime_size_adjustment")
regime_changed         = count("regime_changed")
regime_update_failed   = count("regime_update_failed")
ofi_execution_actions  = Counter(
    e.get("action_label", "standard")
    for e in events
    if e.get("event") == "ofi_execution_action"
)

# Async task death visibility (Tier 3 supervisor)
task_died_events = [e for e in events if e.get("event") == "task_died"]
task_died_count  = len(task_died_events)

# Previously-fixed events (should stay at 0)
periodic_failed   = count("periodic_check_failed")
cb_attr_err       = count("circuit_breaker_attribute_error")

perf_halt_win_rate_live = sum(
    1 for e in events
    if e.get("event") == "performance_halt_win_rate" and e.get("action") != "log_only"
)
perf_halt_win_rate_paper = sum(
    1 for e in events
    if e.get("event") == "performance_halt_win_rate" and e.get("action") == "log_only"
)
perf_halt_drawdown_live = sum(
    1 for e in events
    if e.get("event") == "performance_halt_drawdown" and e.get("action") != "log_only"
)
perf_halt_drawdown_paper = sum(
    1 for e in events
    if e.get("event") == "performance_halt_drawdown" and e.get("action") == "log_only"
)

degraded_mode     = count("charlie_degraded_mode")
ratio_str = (
    f"{degraded_mode / features:.1f}x  (target: <3x)"
    if features > 0
    else "N/A (0 features computed)"
)

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
risk_rejected_reasons = Counter(
    e.get("reason", "unknown")
    for e in events
    if e.get("event") == "risk_rejected"
)
portfolio_blocked_reasons = Counter(
    e.get("reason", "unknown")
    for e in events
    if e.get("event") == "order_blocked_portfolio_risk"
)

cb_trip_events     = [e for e in events if e.get("event") == "circuit_breaker_tripped"]
cb_half_open_events = [e for e in events if e.get("event") == "circuit_breaker_half_open"]
cb_recovered_events = [e for e in events if e.get("event") == "circuit_breaker_recovered"]
cb_init_events      = [e for e in events if e.get("event") == "circuit_breaker_initialized"]

print(f"\n=== SESSION STATS ===")
print(f"  Total log lines  : {total_lines}")
print(f"  strategy_scan_begin: {scans}")

# ============================================================
# INVARIANT CHECKS (Tier 2 — logic violations between events)
# ============================================================
# These are causal invariants: if A happened, B must also have happened.
# A violation means the pipeline silently swallowed something.
_RESET  = "\033[0m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_GREEN  = "\033[92m"

def _color(text, color):
    """Wrap text in ANSI color codes (gracefully degrades on Windows)."""
    try:
        return f"{color}{text}{_RESET}"
    except Exception:
        return text

violations = []
warnings_inv = []

# INV-1: If scans happened but zero features were computed, Binance feed is dead.
if scans > 10 and features == 0:
    violations.append(
        f"INV-1  scans={scans} but binance_features_computed=0 "
        "-- Binance candle/indicator pipeline is broken"
    )

# INV-2: If features were computed but charlie_approved=0 and opp_detected>0,
#         something in Charlie or the gate is silently blocking everything.
if features > 20 and opp_detected > 0 and charlie_approved == 0:
    all_charlie_rejected = count("charlie_gate_rejected")
    coin_flip_rejected   = count("charlie_coin_flip_rejected")
    if all_charlie_rejected == 0 and coin_flip_rejected == 0:
        violations.append(
            f"INV-2  features={features} opp_detected={opp_detected} but "
            "charlie_approved=0 with NO rejection events -- silent gate failure"
        )

# INV-3: If charlie_approved > 0 but order_submitted=0 (and no exec errors),
#         execution service is broken.
exec_errors = count("execution_failed")
if charlie_approved > 0 and order_submitted == 0 and exec_errors == 0:
    violations.append(
        f"INV-3  charlie_approved={charlie_approved} but order_submitted=0 "
        "with execution_failed=0 -- silent execution block"
    )

# INV-4: order_submitted > charlie_approved is impossible (each order needs approval).
if order_submitted > charlie_approved and charlie_approved > 0:
    violations.append(
        f"INV-4  order_submitted={order_submitted} > charlie_approved={charlie_approved} "
        "-- accounting mismatch, investigate dedup logic"
    )

# INV-5: If any task_died events exist, that is always a CRITICAL violation.
if task_died_count > 0:
    for _td in task_died_events:
        violations.append(
            f"INV-5  task_died: task={_td.get('task_name','?')} "
            f"error={str(_td.get('error','?'))[:120]}"
        )

# INV-6: OFI feed degradation rate. If > 10% of evaluated opps triggered
#         ofi_feed_degraded, the orderbook pipeline needs investigation.
if opps_evaluated > 5 and ofi_feed_degraded > 0:
    pct = 100.0 * ofi_feed_degraded / opps_evaluated
    if pct > 10:
        violations.append(
            f"INV-6  ofi_feed_degraded={ofi_feed_degraded} / opps_evaluated={opps_evaluated} "
            f"= {pct:.1f}% (threshold: 10%) -- OFI orderbook pipeline degraded"
        )
    else:
        warnings_inv.append(
            f"INV-6  ofi_feed_degraded={ofi_feed_degraded} / opps_evaluated={opps_evaluated} "
            f"= {pct:.1f}% (minor, < 10% threshold)"
        )

# INV-7: If binance_feed_unhealthy fired, the ws supervisor caught it;
#         but if it fires AND scans continued, something is wrong.
binance_feed_unhealthy = count("binance_feed_unhealthy")
if binance_feed_unhealthy > 0 and scans > 5:
    violations.append(
        f"INV-7  binance_feed_unhealthy={binance_feed_unhealthy} but scans kept running ({scans}) "
        "-- supervisor may not have halted scan loop on feed failure"
    )

print(f"\n--- INVARIANT CHECKS ---")
if violations:
    for v in violations:
        print(f"  {_color('VIOLATION', _RED)}  {v}")
else:
    print(f"  {_color('ALL INVARIANTS HOLD', _GREEN)}")
if warnings_inv:
    for w in warnings_inv:
        print(f"  {_color('WARN', _YELLOW)}      {w}")

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
          f"  (paper-mode win-rate warning — CB not tripped, safe to ignore)")
if perf_halt_drawdown_paper > 0:
    print(f"  INFO          performance_halt_drawdown [paper log_only]: {perf_halt_drawdown_paper}"
          f"  (cross-session historical drawdown — CB not tripped, safe to ignore)")

print(f"\n--- ERROR ORDERS (DB total, all sessions) ---")
print(f"  order_state='ERROR' in DB  : {_error_orders_total}")
if _error_orders_by_market:
    for key, n in list(_error_orders_by_market.items())[:5]:
        print(f"    [{n}x] {key}")
print(f"  order_state_set_to_error   : {order_error_transitions}  (this session log)")

print(f"\n--- MARKET BLOCKING (this session) ---")
print(f"  market_blocked[static_blocked_markets]       : {market_blocked_static}")
print(f"  market_blocked[performance_guard_auto_block] : {market_blocked_perf}")
print(f"  performance_guard_checked                    : {perf_guard_checked}  (re-evaluates each call — must be > 0 after restart)")
print(f"  market_auto_blocked_performance              : {market_auto_blocked}  (guard fired and will block on next restart)")

print(f"\n--- OFI SIGNAL (this session) ---")
print(f"  ofi_signal_confirmed : {ofi_signal_confirmed}")
print(f"  ofi_conflict         : {ofi_conflict}  (signals where OFI disagrees with Charlie; size halved)")
_ofi_deg_label = (
    _color(f"{ofi_feed_degraded}  <-- INVESTIGATE", _RED)
    if ofi_feed_degraded > 0 else str(ofi_feed_degraded)
)
print(f"  ofi_feed_degraded    : {_ofi_deg_label}  (Tier 2: absent ofi_bids/ofi_asks for 5+ consecutive evaluations)")

print(f"\n--- ASYNC TASK HEALTH (Tier 3 supervisor) ---")
if task_died_count == 0:
    print(f"  task_died : {_color('0  OK', _GREEN)}")
else:
    print(f"  task_died : {_color(str(task_died_count) + '  CRITICAL -- background tasks crashed', _RED)}")
    for _td in task_died_events[:5]:
        print(f"    task={_td.get('task_name','?')}  error={str(_td.get('error','?'))[:100]}")
binance_feed_unhealthy_label = (
    _color(str(binance_feed_unhealthy), _RED) if binance_feed_unhealthy > 0 else str(binance_feed_unhealthy)
)
print(f"  binance_feed_unhealthy : {binance_feed_unhealthy_label}")

print(f"\n--- ML FEATURE GATES (Sessions 1-4) ---")
print(f"  meta_gate_approved   : {meta_gate_approved}")
print(f"  meta_gate_rejected   : {meta_gate_rejected}", end="")
if meta_gate_approved + meta_gate_rejected > 0:
    _gate_pct = 100 * meta_gate_rejected / (meta_gate_approved + meta_gate_rejected)
    print(f"  ({_gate_pct:.1f}% rejected — target: <50%)", end="")
print()
if meta_gate_dec_approved + meta_gate_dec_rejected > 0:
    _dec_total = meta_gate_dec_approved + meta_gate_dec_rejected
    _dec_pct = 100 * meta_gate_dec_rejected / _dec_total
    _proba_str = f"  avg_proba={_meta_gate_avg_proba}" if _meta_gate_avg_proba is not None else ""
    print(f"  meta_gate_decision   : {_dec_total} decisions  "
          f"({meta_gate_dec_approved} approved / {meta_gate_dec_rejected} rejected "
          f"= {_dec_pct:.1f}% rejected){_proba_str}")
print(f"  market_blocked_tag   : {market_blocked_tag}  (Session 3: tag-blocklist filter)")
print(f"  regime_size_adj      : {regime_size_adj}  (Session 2: dynamic size adjustments)")
print(f"  regime_changed       : {regime_changed}  (Session 2: regime transitions this session)")
if regime_update_failed > 0:
    print(f"  WARN regime_update_failed: {regime_update_failed}  (check Binance connectivity)")
if ofi_execution_actions:
    _ofi_total = sum(ofi_execution_actions.values())
    print(f"  ofi_execution_action : {_ofi_total} total (Session 4: log-only policy)")
    for _action, _cnt in sorted(ofi_execution_actions.items()):
        print(f"    +-- {_action}: {_cnt}")
else:
    print(f"  ofi_execution_action : 0  (Session 4 log-only — no orders reached policy yet)")

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

if risk_rejected_reasons:
    first_rr = next((e for e in events if e.get("event") == "risk_rejected"), None)
    if first_rr and first_rr.get("cb_state"):
        print(f"  last known CB state when blocking: state={first_rr.get('cb_state')}  "
              f"half_open_max={first_rr.get('half_open_max_pct')}%  "
              f"peak_equity={first_rr.get('peak_equity')}")

print(f"\n--- REJECTION BREAKDOWN ---")
found_any_rejection = False

total_rr = sum(risk_rejected_reasons.values())
if total_rr > 0:
    found_any_rejection = True
    print(f"  risk_rejected (circuit_breaker): {total_rr}  <-- fires BEFORE Charlie")
    for reason, c in risk_rejected_reasons.most_common():
        print(f"    +-- {reason}: {c}")

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
                p_win_summary = f"  (p_win range: {min(p_wins):.3f}-{max(p_wins):.3f})"
            print(f"  {ev}: {c}{p_win_summary}")
            for reason, rc in reasons.most_common():
                print(f"    +-- reason={reason}: {rc}")
        else:
            print(f"  {ev}: {c}")

if portfolio_blocked_reasons:
    found_any_rejection = True
    total_pb = sum(portfolio_blocked_reasons.values())
    print(f"  order_blocked_portfolio_risk: {total_pb}")
    for reason, c in portfolio_blocked_reasons.most_common():
        print(f"    +-- {reason}: {c}")

if opportunity_skip_reasons:
    found_any_rejection = True
    for reason, c in opportunity_skip_reasons.most_common():
        print(f"  opportunity_skipped[{reason}]: {c}")

if not found_any_rejection:
    print("  (none -- if charlie_gate_approved=0 check for charlie_gate_rejected events)")

print(f"\n--- ADVANCED FEATURES ---")
_arb_found = count("yes_no_arb_found")
_snipe_would = count("snipe_would_fire")
_snipe_attempt = count("snipe_attempt")
_oracle_window = count("oracle_window_detected")
print(f"  yes_no_arb_found: {_arb_found}")
print(f"  snipe_would_fire: {_snipe_would}")
print(f"  snipe_attempt: {_snipe_attempt}")
print(f"  oracle_window_detected: {_oracle_window}")

# ---------- final verdict ----------
all_ok = ok_fixed and signals_ok and not violations
print()
if all_ok:
    print("=== ALL CHECKS PASSED -- run the Kelly sweep next ===")
    print("  python experiments/sweep_kelly_and_edge.py --log logs/production.log")
else:
    print("=== CHECKS FAILED -- DO NOT RUN SWEEP YET ===")
    if violations:
        print("  Invariant violations detected — see INVARIANT CHECKS section above.")
    if not ok_fixed:
        print("  Previously-fixed errors have returned.")
    if not signals_ok:
        total_circuit_blocked = sum(risk_rejected_reasons.values())
        if opp_detected == 0:
            print("  No opportunities detected. Check strategy engine and min_edge config.")
        elif total_circuit_blocked > 0 and charlie_approved == 0:
            print("  Circuit breaker blocked all opportunities BEFORE Charlie was called.")
            for reason, c in risk_rejected_reasons.most_common():
                print(f"    +-- {reason}: {c}")
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
_cal_csv = Path("data/calibration_dataset_v2.csv")
_legacy_cal_csv = Path("data/calibration_dataset.csv")
import csv as _csv
if _cal_csv.exists():
    _cal_p, _cal_a = [], []
    with open(_cal_csv, "r", newline="") as _cf:
        for _row in _csv.DictReader(_cf):
            try:
                _cal_p.append(float(_row["raw_yes_prob"]))
                _cal_a.append(int(_row["actual_yes_outcome"]))
            except (KeyError, ValueError):
                pass
    _cal_n = len(_cal_p)
    print(f"  Calibration samples: {_cal_n}")
    if _cal_n > 0:
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
        print(f"  [READY] CALIBRATION READY -- run: python scripts/fit_calibration.py")
    else:
        _remaining = 100 - _cal_n
        print(f"  [..] Need {_remaining} more samples before fitting (target: 100)")
        print(f"     At ~20-30 settlements/day -> ~{max(1, _remaining // 25)} more days")
else:
    print("  (no schema-v2 calibration data yet — wait for scored observations to resolve)")
if _legacy_cal_csv.exists():
    with open(_legacy_cal_csv, "r", newline="") as _legacy_cf:
        _legacy_rows = sum(1 for _ in _csv.DictReader(_legacy_cf))
    print(f"  Legacy archive rows retained: {_legacy_rows}")

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
