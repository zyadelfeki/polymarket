"""
Live terminal dashboard for the Polymarket bot.

Run separately from the main bot process:
    python monitoring/live_dashboard.py

Reads the latest JSON log file and SQLite DB.
Refreshes every 5 seconds.  No web server, no dependencies beyond stdlib + structlog.

Zero impact on main bot performance — read-only DB queries only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Configurable paths
DB_PATH     = _ROOT / "data" / "trading.db"
LOG_DIR     = _ROOT / "logs"
HEALTH_URL  = "http://localhost:8765/health"
REFRESH_S   = 5


def _latest_log() -> Path | None:
    """Find the most-recently-modified .log file in logs/."""
    logs = list(LOG_DIR.glob("*.log")) if LOG_DIR.exists() else []
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


def _tail_json_events(log_path: Path, n: int = 500) -> list[dict]:
    """Read last N lines of a JSON log, parse what we can."""
    events = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        for line in lines:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return events


def _db_stats() -> dict:
    """Fast read-only stats from the SQLite DB."""
    stats = {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)

        # Equity from ledger
        row = conn.execute(
            "SELECT balance FROM accounts WHERE account_name='Cash' LIMIT 1"
        ).fetchone()
        stats["equity"] = abs(float(row[0])) if row else None

        # Open orders
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(CAST(size AS REAL)*CAST(price AS REAL)),0)"
            " FROM order_tracking WHERE order_state NOT IN ('SETTLED','CANCELLED','ERROR')"
        ).fetchone()
        stats["open_count"]    = r[0] if r else 0
        stats["open_exposure"] = round(r[1], 2) if r else 0.0

        # Settled PnL
        r2 = conn.execute(
            "SELECT COALESCE(SUM(CAST(pnl AS REAL)),0) FROM order_tracking"
            " WHERE order_state='SETTLED'"
        ).fetchone()
        stats["settled_pnl"] = round(float(r2[0]), 4) if r2 else 0.0

        # Order counts by state
        rows = conn.execute(
            "SELECT order_state, COUNT(*) FROM order_tracking GROUP BY order_state"
        ).fetchall()
        stats["states"] = {r[0]: r[1] for r in rows}

        # Win/loss from settled
        r3 = conn.execute(
            "SELECT COUNT(*) FROM order_tracking"
            " WHERE order_state='SETTLED' AND CAST(pnl AS REAL)>0"
        ).fetchone()
        r4 = conn.execute(
            "SELECT COUNT(*) FROM order_tracking"
            " WHERE order_state='SETTLED' AND CAST(pnl AS REAL)<=0"
        ).fetchone()
        stats["wins"]   = r3[0] if r3 else 0
        stats["losses"] = r4[0] if r4 else 0

        conn.close()
    except Exception as exc:
        stats["db_error"] = str(exc)
    return stats


def _health_check() -> dict | None:
    """Quick HTTP GET to the health endpoint (optional)."""
    try:
        import urllib.request
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def render_dashboard() -> None:
    """Clear screen and render single dashboard frame."""
    log_path = _latest_log()
    events   = _tail_json_events(log_path) if log_path else []
    stats    = _db_stats()
    health   = _health_check()

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    bar = "=" * 70

    os.system("cls" if os.name == "nt" else "clear")
    print(bar)
    print(f"  POLYMARKET BOT  —  LIVE DASHBOARD          {now_str}")
    print(bar)

    # --- Equity & exposure --------------------------------------------------
    equity_str = f"${stats.get('equity', '?'):>10,.2f} USDC" if stats.get("equity") is not None else "N/A"
    print(f"  Equity:          {equity_str}")
    print(f"  Open Orders:     {stats.get('open_count', 0):>5}  (${stats.get('open_exposure', 0):,.2f} exposure)")
    total = stats.get("wins", 0) + stats.get("losses", 0)
    wr_str = f"{stats['wins']/(total):.1%}" if total > 0 else "N/A"
    print(f"  Settled PnL:     ${stats.get('settled_pnl', 0):>10,.4f} USDC")
    print(f"  Win Rate:        {wr_str}  ({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)")

    # --- Health endpoint (if available) ------------------------------------
    if health:
        hstatus  = health.get("status", "?")
        uptime   = health.get("uptime_s", 0)
        hours, rem = divmod(int(uptime), 3600)
        mins = rem // 60
        print(f"  Bot Status:      {hstatus.upper()}  (uptime {hours}h {mins}m)")
        svc_cbs = health.get("service_circuit_breakers", {})
        if svc_cbs:
            cb_str = "  ".join(f"{k}:{v}" for k, v in svc_cbs.items())
            print(f"  Service CBs:     {cb_str}")
        if health.get("drawdown_halted"):
            print("  *** DRAWDOWN KILL SWITCH ACTIVE — TRADING HALTED ***")
    else:
        print("  Health endpoint: not reachable (bot may not be running)")

    # --- Order state breakdown ----------------------------------------------
    print()
    print(f"  ORDER STATES:")
    for state, cnt in sorted(stats.get("states", {}).items()):
        print(f"    {state:<12} {cnt:>6}")

    # --- Recent events from log --------------------------------------------
    print()
    print(f"  RECENT EVENTS (last log: {log_path.name if log_path else 'none'}):")
    _SHOW_EVENTS = {
        "charlie_coin_flip_rejected",
        "charlie_gate_approved",
        "charlie_gate_blocked",
        "binance_features_computed",
        "portfolio_risk_check",
        "order_submitted",
        "order_blocked_portfolio_risk",
        "order_blocked_no_charlie_signal",
        "settlement_scan_complete",
        "drawdown_kill_switch_triggered",
        "circuit_breaker_trip",
        "order_settled_live",
    }

    shown = [e for e in events if e.get("event") in _SHOW_EVENTS][-12:]
    for ev in shown:
        ts_raw = ev.get("timestamp", ev.get("time", ""))
        ts = ts_raw[-12:] if ts_raw else "??:??:??"
        name = ev.get("event", "")
        extra = ""
        if name == "charlie_gate_approved":
            extra = f"  edge={ev.get('edge','')}  p_win={ev.get('p_win','')}  size={ev.get('size','')}  regime={ev.get('regime','')}"
        elif name == "charlie_coin_flip_rejected":
            extra = f"  p_win={ev.get('p_win','')} (BLOCKED — coin flip)"
        elif name == "charlie_gate_blocked":
            extra = f"  reason={ev.get('reason','')}  market={str(ev.get('market_id',''))[:8]}.."
        elif name == "order_submitted":
            extra = f"  market={str(ev.get('market_id',''))[:8]}..  side={ev.get('side','')}"
        elif name == "settlement_scan_complete":
            extra = f"  resolved={ev.get('resolved_count',0)}  checked={ev.get('open_positions_checked',0)}"
        elif name == "portfolio_risk_check":
            extra = f"  cat={ev.get('category','')}  total_exp={ev.get('total_exposure_pct','')}  approved={ev.get('kelly_approved','')}"
        elif name == "binance_features_computed":
            extra = f"  rsi={ev.get('rsi_14','')}  macd={ev.get('macd','')}  imbalance={ev.get('book_imbalance','')}"
        elif name == "order_settled_live":
            pnl_val = float(ev.get("pnl", 0) or 0)
            extra = f"  pnl={'+'if pnl_val>=0 else ''}{pnl_val:.4f}  market={str(ev.get('market_id',''))[:8]}.."
        elif name == "drawdown_kill_switch_triggered":
            extra = f"  *** drawdown={ev.get('drawdown_pct','')} ***"
        print(f"  [{ts}] {name}{extra}")

    # --- Event frequency summary -------------------------------------------
    counts = Counter(e.get("event") for e in events)
    print()
    print(f"  EVENT COUNTS (last {len(events)} log lines):")
    _KEY_COUNTS = [
        ("charlie_coin_flip_rejected",   "Charlie noise blocked"),
        ("charlie_gate_approved",        "Charlie approved"),
        ("binance_features_computed",    "Binance features"),
        ("portfolio_risk_check",         "Portfolio risk checks"),
        ("order_submitted",              "Orders submitted"),
        ("settlement_scan_complete",     "Settlement scans"),
        ("order_settled_live",           "Orders settled"),
    ]
    for evt, label in _KEY_COUNTS:
        cnt = counts.get(evt, 0)
        flag = " ✗" if cnt == 0 and evt in (
            "settlement_scan_complete", "charlie_gate_approved"
        ) else ""
        print(f"    {label:<30} {cnt:>5}{flag}")

    print(bar)
    print(f"  Refreshing every {REFRESH_S}s  |  Ctrl+C to exit")


def main() -> None:
    print("Starting dashboard… (press Ctrl+C to quit)")
    time.sleep(0.5)
    while True:
        try:
            render_dashboard()
        except KeyboardInterrupt:
            print("\nDashboard closed.")
            sys.exit(0)
        except Exception as exc:
            print(f"Dashboard render error: {exc}")
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    main()
