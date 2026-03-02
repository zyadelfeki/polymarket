"""
diagnose_winrate.py — per-market win-rate and PnL breakdown.

Uses PRAGMA table_info to introspect the actual schema so it survives
column additions without hardcoded column-name errors.

Usage:
    python scripts/diagnose_winrate.py [--db data/trading.db] [--n 50]
    python scripts/diagnose_winrate.py --market 1450993   # single market drill-down
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_columns(conn: sqlite3.Connection, table: str) -> set:
    """Return the set of real column names for *table*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _f(val: Any, fmt: str = ".4f") -> str:
    if val is None:
        return "   n/a"
    try:
        return format(float(val), fmt)
    except (TypeError, ValueError):
        return str(val)[:8]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_settled(
    db_path: str,
    n: int = 200,
    market_id: Optional[str] = None,
) -> List[Dict]:
    """Load last *n* settled orders from order_tracking, schema-safely."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cols = _get_columns(conn, "order_tracking")

    want = [
        "market_id", "order_state", "pnl", "size", "price",
        "outcome", "strategy", "mode",
    ]
    optional = [
        "charlie_p_win", "charlie_conf", "charlie_regime",
        "model_votes", "notes", "opened_at", "closed_at",
    ]
    select_cols = [c for c in want if c in cols]
    for col in optional:
        if col in cols:
            select_cols.append(col)

    market_clause = "AND market_id = ?" if market_id else ""
    params: list = [market_id] if market_id else []

    query = f"""
        SELECT {', '.join(select_cols)},
               COALESCE(closed_at, opened_at) AS sort_key
        FROM order_tracking
        WHERE order_state IN ('SETTLED', 'RESOLVED')
          AND pnl IS NOT NULL
          {market_clause}
        ORDER BY sort_key DESC
        LIMIT ?
    """
    params.append(n)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_trade_table(rows: List[Dict], title: str = "Last trades"):
    has_p_win  = any("charlie_p_win" in r for r in rows)
    has_conf   = any("charlie_conf" in r for r in rows)
    has_regime = any("charlie_regime" in r for r in rows)

    hdr = [f"{'#':>3}", f"{'W/L':>4}", f"{'PnL':>8}", f"{'price':>6}", f"{'size':>7}"]
    if has_p_win:
        hdr.append(f"{'p_win':>6}")
    if has_conf:
        hdr.append(f"{'conf':>6}")
    if has_regime:
        hdr.append(f"{'regime':>8}")
    hdr += [f"{'out':>4}", f"{'closed':>16}", "market_id"]

    print(f"\n=== {title} ===")
    sep = "  "
    print(sep.join(hdr))
    print("-" * (len(sep.join(hdr)) + 10))

    wins = 0
    total = 0
    for i, r in enumerate(rows, 1):
        pnl = float(r["pnl"]) if r.get("pnl") is not None else None
        won = pnl is not None and pnl > 0
        if pnl is not None:
            wins += int(won)
            total += 1
        sign = "WIN " if won else ("LOSS" if pnl is not None and pnl < 0 else "PUSH")
        vals = [
            f"{i:>3}", f"{sign:>4}", f"{_f(pnl):>8}",
            f"{_f(r.get('price'), '.3f'):>6}", f"{_f(r.get('size'), '.2f'):>7}",
        ]
        if has_p_win:
            vals.append(f"{_f(r.get('charlie_p_win'), '.3f'):>6}")
        if has_conf:
            vals.append(f"{_f(r.get('charlie_conf'), '.3f'):>6}")
        if has_regime:
            vals.append(f"{str(r.get('charlie_regime') or ''):>8}")
        closed = str(r.get("sort_key") or r.get("closed_at") or "")[:16]
        mkt = r.get("market_id") or "unknown"
        vals += [f"{str(r.get('outcome') or '')[:4]:>4}", f"{closed:>16}", mkt]
        print(sep.join(vals))

    if total:
        print(f"\nWin rate: {wins}/{total} = {wins/total:.1%}")
    else:
        print("\nNo settled trades with PnL found.")


def print_market_breakdown(rows: List[Dict], min_trades: int = 3):
    """Per-market win/loss summary, worst PnL first."""
    per_market: Dict[str, Any] = defaultdict(lambda: {
        "wins": 0, "losses": 0, "pnl": 0.0, "p_win_vals": [], "trades": 0
    })
    for r in rows:
        mkt = r.get("market_id") or "unknown"
        pnl = float(r["pnl"]) if r.get("pnl") is not None else None
        if pnl is None:
            continue
        per_market[mkt]["trades"] += 1
        per_market[mkt]["pnl"] += pnl
        if pnl > 0:
            per_market[mkt]["wins"] += 1
        else:
            per_market[mkt]["losses"] += 1
        pw = r.get("charlie_p_win")
        if pw is not None:
            try:
                per_market[mkt]["p_win_vals"].append(float(pw))
            except (TypeError, ValueError):
                pass

    markets = [
        (mkt, d) for mkt, d in per_market.items() if d["trades"] >= min_trades
    ]
    markets.sort(key=lambda x: x[1]["pnl"])

    if not markets:
        print(f"\nNo markets with >= {min_trades} trades in sample.")
        return

    print(f"\n=== Per-market breakdown (>= {min_trades} trades, worst first) ===")
    print(f"{'market_id':<22}  {'T':>4}  {'W':>4}  {'L':>4}  {'WR%':>6}  {'total_pnl':>12}  {'avg_p_win':>10}")
    print("-" * 82)
    for mkt, d in markets:
        t = d["trades"]
        w = d["wins"]
        wr = f"{w/t:.0%}" if t else "  n/a"
        pnl_s = f"{d['pnl']:>12.4f}"
        avg_pw = f"{sum(d['p_win_vals'])/len(d['p_win_vals']):.3f}" if d["p_win_vals"] else "   n/a"
        flag = " ← CHRONIC LOSER" if t >= 5 and w / t < 0.25 else ""
        print(f"{mkt:<22}  {t:>4}  {w:>4}  {t-w:>4}  {wr:>6}  {pnl_s}  {avg_pw:>10}{flag}")


def print_rolling_stats(rows: List[Dict]):
    print("\n=== Rolling win rate ===")
    for window in (20, 50, 100):
        if len(rows) >= window:
            recent = rows[:window]  # DESC order → newest first
            w = sum(1 for r in recent if r.get("pnl") is not None and float(r["pnl"]) > 0)
            line = f"  Last {window:>3}: {w:>3}/{window} = {w/window:.1%}"
            if window == 20 and w / window < 0.35:
                line += "  ← BELOW HALT THRESHOLD (0.35)"
            print(line)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Per-market win-rate and PnL diagnostic")
    parser.add_argument("--db", default="data/trading.db")
    parser.add_argument("--n", type=int, default=50, help="Last N settled trades")
    parser.add_argument("--market", default=None, help="Drill down to a single market_id")
    parser.add_argument("--min-trades", type=int, default=3, help="Min trades/market for breakdown")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}")
        raise SystemExit(1)

    rows = load_settled(str(db_path), n=args.n, market_id=args.market)
    if not rows:
        print("No settled/resolved trades with PnL found.")
        raise SystemExit(0)

    title = f"Last {len(rows)} settled trades"
    if args.market:
        title += f" — market {args.market}"

    print_trade_table(rows, title=title)
    if not args.market:
        print_market_breakdown(rows, min_trades=args.min_trades)
    print_rolling_stats(rows)


if __name__ == "__main__":
    main()
            if count >= 3:
                break
    except Exception:
        pass
