#!/usr/bin/env python3
"""
PnL attribution by market tag dimension — Session 3.

Joins settled trades from trading.db against tags in market_tags.db and
reports win-rate, average PnL, and expected-value per tag group.  Use this
to identify which tag patterns are profitable and which should be added to
MARKET_TAG_BLOCKLIST in config_production.py.

Usage:
    python scripts/pnl_by_tags.py
    python scripts/pnl_by_tags.py --min-trades 3
    python scripts/pnl_by_tags.py --dim event_type
    python scripts/pnl_by_tags.py --export reports/pnl_by_tags.csv

Columns in the report:
    tag_dim   -- tag dimension (e.g. "event_type")
    tag_value -- value for that dimension (e.g. "election")
    trades    -- number of settled trades with this tag
    wins      -- number of winning trades
    win_rate  -- fraction of winning trades
    avg_pnl   -- average PnL per trade (USDC)
    total_pnl -- sum of all PnL for this group
    ev        -- estimated expected value (avg_pnl * win_rate heuristic)
"""
import argparse
import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_TRADING_DB = _REPO_ROOT / "data" / "trading.db"
_TAGS_DB    = _REPO_ROOT / "data" / "market_tags.db"

# Tag dimensions to break down PnL by
_TAG_DIMS = ["asset", "event_type", "horizon", "outcome_type", "info_edge_needed"]


def _load_settled_trades() -> list[dict]:
    """
    Returns a list of dicts with keys: market_id, pnl, outcome (WIN/LOSS).
    Only includes rows where order_state = 'SETTLED' and pnl_realised IS NOT NULL.
    """
    if not _TRADING_DB.exists():
        return []
    with sqlite3.connect(str(_TRADING_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT market_id,
                   COALESCE(pnl_realised, 0.0)     AS pnl,
                   CASE WHEN COALESCE(pnl_realised, 0) > 0 THEN 'WIN' ELSE 'LOSS' END AS outcome
            FROM order_tracking
            WHERE order_state = 'SETTLED'
              AND pnl_realised IS NOT NULL
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _load_tags() -> dict[str, dict]:
    """Returns {market_id: tag_dict} from market_tags.db."""
    if not _TAGS_DB.exists():
        return {}
    with sqlite3.connect(str(_TAGS_DB)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT market_id, asset, event_type, horizon, outcome_type, "
            "       asymmetry_flag, info_edge_needed "
            "FROM market_tags"
        ).fetchall()
    return {r["market_id"]: dict(r) for r in rows}


def _build_report(
    trades: list[dict],
    tags: dict[str, dict],
    dims: list[str],
    min_trades: int,
) -> list[dict]:
    """
    Groups trades by each (dim, value) pair and computes statistics.
    Trades without a tag entry are grouped under '<untagged>'.
    """
    # Accumulate: key=(dim, value)  value=[pnl, ...]
    groups: dict[tuple, list[float]] = defaultdict(list)

    for trade in trades:
        mid = trade["market_id"]
        tag = tags.get(mid, {})
        for dim in dims:
            val = tag.get(dim, "<untagged>") if tag else "<untagged>"
            if val is None:
                val = "<untagged>"
            groups[(dim, str(val))].append(float(trade["pnl"]))

    report = []
    for (dim, val), pnl_list in sorted(groups.items()):
        n = len(pnl_list)
        if n < min_trades:
            continue
        wins = sum(1 for p in pnl_list if p > 0)
        win_rate = wins / n if n > 0 else 0.0
        avg_pnl = sum(pnl_list) / n
        total_pnl = sum(pnl_list)
        report.append({
            "tag_dim":   dim,
            "tag_value": val,
            "trades":    n,
            "wins":      wins,
            "win_rate":  round(win_rate, 4),
            "avg_pnl":   round(avg_pnl, 4),
            "total_pnl": round(total_pnl, 4),
        })

    # Sort by total_pnl ascending so worst groups show first
    report.sort(key=lambda r: r["total_pnl"])
    return report


def _print_report(rows: list[dict], dim_filter: str | None) -> None:
    if not rows:
        print("  (no data — run scripts/tag_market_questions.py first, or wait for settlements)")
        return

    current_dim = None
    for row in rows:
        if dim_filter and row["tag_dim"] != dim_filter:
            continue
        if row["tag_dim"] != current_dim:
            current_dim = row["tag_dim"]
            print(f"\n  ── {current_dim} ──")
            print(f"  {'value':<22}  {'n':>5}  {'wins':>5}  {'win%':>6}  "
                  f"{'avg_pnl':>9}  {'total_pnl':>11}")
            print(f"  {'─'*22}  {'─'*5}  {'─'*5}  {'─'*6}  {'─'*9}  {'─'*11}")
        win_pct = f"{100 * row['win_rate']:.1f}%"
        avg_sign = "+" if row["avg_pnl"] >= 0 else ""
        tot_sign = "+" if row["total_pnl"] >= 0 else ""
        print(
            f"  {row['tag_value']:<22}  {row['trades']:>5}  {row['wins']:>5}  "
            f"{win_pct:>6}  {avg_sign}{row['avg_pnl']:>8.4f}  "
            f"{tot_sign}{row['total_pnl']:>10.4f}"
        )


def _export_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["tag_dim", "tag_value", "trades", "wins", "win_rate", "avg_pnl", "total_pnl"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n  Exported {len(rows)} rows → {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PnL attribution by market tag dimension"
    )
    parser.add_argument("--min-trades", type=int, default=2,
                        help="Minimum settled trades to show a group (default: 2)")
    parser.add_argument("--dim", default=None,
                        help=f"Report only this dimension (choices: {', '.join(_TAG_DIMS)})")
    parser.add_argument("--export", default=None,
                        help="Export results to CSV at this path")
    args = parser.parse_args()

    print("\n=== PnL BY TAG GROUP ===")

    trades = _load_settled_trades()
    if not trades:
        print(f"\n  No settled trades found in {_TRADING_DB}")
        print("  (data/trading.db may not exist yet, or no settled orders)")
        return
    print(f"  Settled trades loaded: {len(trades)}")

    tags = _load_tags()
    tagged_count = sum(1 for t in trades if t["market_id"] in tags)
    print(f"  Markets with tags    : {len(tags)}  ({tagged_count}/{len(trades)} trades have tagss)")

    if not tags:
        print(f"\n  No tags found in {_TAGS_DB}")
        print("  Run: python scripts/tag_market_questions.py")
        return

    dims = [args.dim] if args.dim else _TAG_DIMS
    report = _build_report(trades, tags, dims, args.min_trades)

    _print_report(report, args.dim)

    if args.export:
        _export_csv(report, Path(args.export))

    # Advisory: flag groups with negative avg_pnl as blocklist candidates
    bad_groups = [r for r in report if r["avg_pnl"] < 0 and r["trades"] >= 5]
    if bad_groups:
        print(f"\n  BLOCKLIST CANDIDATES (avg_pnl < 0, trades >= 5):")
        for r in bad_groups:
            entry = '{' + f'"{r["tag_dim"]}": "{r["tag_value"]}"' + '}'
            print(f"    {entry}  ({r['trades']} trades, avg_pnl={r['avg_pnl']:+.4f})")
        print("  Add these to MARKET_TAG_BLOCKLIST in config_production.py")


if __name__ == "__main__":
    main()
