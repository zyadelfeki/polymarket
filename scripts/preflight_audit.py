"""Pre-flight audit — read-only diagnostic. Run from the polymarket directory."""
import sqlite3
import json
import sys
from pathlib import Path
from collections import Counter

print("=== 1. DB PEAK EQUITY ===")
try:
    conn = sqlite3.connect("data/trading.db")
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall():
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for col in cols:
            if any(kw in col.lower() for kw in ["peak", "equity", "balance", "bankroll"]):
                try:
                    rows = conn.execute(
                        f'SELECT "{col}" FROM "{table}" ORDER BY rowid DESC LIMIT 3'
                    ).fetchall()
                    print(f"  {table}.{col}: {rows}")
                except Exception as e:
                    print(f"  {table}.{col}: QUERY_ERROR {e}")
except Exception as e:
    print(f"  DB open error: {e}")
    conn = None

print()
print("=== 2. ERROR ORDER BREAKDOWN ===")
if conn:
    try:
        rows = conn.execute("""
            SELECT COALESCE(notes, 'NULL'), COUNT(*) as cnt
            FROM order_tracking WHERE order_state = 'ERROR'
            GROUP BY notes ORDER BY cnt DESC LIMIT 10
        """).fetchall()
        for r in rows:
            print(f"  cnt={r[1]:>8}  notes={str(r[0])[:90]}")
        if not rows:
            print("  (no ERROR rows found)")
    except Exception as e:
        print(f"  Query error: {e}")

print()
print("=== 3. ORDER STATE TOTALS ===")
if conn:
    try:
        rows = conn.execute("""
            SELECT order_state, COUNT(*) as cnt
            FROM order_tracking
            GROUP BY order_state ORDER BY cnt DESC
        """).fetchall()
        for r in rows:
            print(f"  {r[0]:>20}: {r[1]:>10}")
    except Exception as e:
        print(f"  Query error: {e}")

print()
print("=== 4. STUCK SUBMITTED ORDERS ===")
if conn:
    try:
        stuck = conn.execute("""
            SELECT COUNT(*) FROM order_tracking
            WHERE order_state = 'SUBMITTED'
            AND datetime(opened_at) < datetime('now', '-10 minutes')
        """).fetchone()
        print(f"  Stuck SUBMITTED orders: {stuck[0]}")
    except Exception as e:
        print(f"  Query error: {e}")

print()
print("=== 5. LAST LOG EVENTS ===")
for log_path in ["logs/paper-session.log", "bot_run.log"]:
    log = Path(log_path)
    if log.exists():
        counts: Counter = Counter()
        with open(log, errors="replace") as f:
            for line in f:
                try:
                    counts[json.loads(line).get("event", "")] += 1
                except Exception:
                    pass
        print(f"  Source: {log_path} ({log.stat().st_size / 1024:.1f} KB)")
        for ev in [
            "performance_halt_drawdown",
            "charlie_degraded_mode",
            "charlie_gate_approved",
            "charlie_coin_flip_rejected",
            "binance_features_computed",
            "order_submitted",
            "circuit_breaker_attribute_error",
            "paper_peak_equity_reset",
        ]:
            print(f"    {ev}: {counts[ev]}")
        break
else:
    print("  No log file found")

print()
print("=== 6. CHARLIE CONSENSUS THRESHOLD ===")
for fname in [
    "integrations/charlie_booster.py",
    "integrations/charlie_intelligence.py",
]:
    p = Path(fname)
    if p.exists():
        src = p.read_text(errors="replace")
        for i, line in enumerate(src.splitlines(), 1):
            if any(
                kw in line.lower()
                for kw in ["consensus", "threshold", "min_confidence", "required_models", "agreement", "min_models"]
            ):
                print(f"  {fname}:{i}: {line.strip()}")
    else:
        print(f"  NOT FOUND: {fname}")

if conn:
    conn.close()
print()
print("=== DONE ===")
