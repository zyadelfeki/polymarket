"""
experiments/rolling_kelly_optimizer.py
========================================
Rolling Kelly optimizer: sweep 16 Kelly/risk parameter combos over the
most recent N days of realized trades and persist the best-performing
config so ``main.py`` picks it up on the next restart.

Usage
-----
    python experiments/rolling_kelly_optimizer.py
    python experiments/rolling_kelly_optimizer.py --window 14
    python experiments/rolling_kelly_optimizer.py --log logs/production.log --window 7

Output
------
* ``config/kelly_config_snapshot_{YYYY-MM-DD}.yaml``  – human-readable archive.
* ``config/kelly_live.json`` – machine-readable; loaded by ``main.py`` at startup.
  Format matches the existing ``kelly_config_loaded_from_optimizer`` path.

Design notes
------------
* Reuses the same 16-combo parameter grid as ``sweep_kelly_and_edge.py``.
* Only combos with at least ``MIN_SETTLED_TRADES`` settled trades are ranked
  to avoid promoting configs that never fired in the window.
* Winner is selected by descending Sharpe; ties broken by ascending drawdown.
* If no combo has MIN_SETTLED_TRADES trades, the script exits with a warning
  and does NOT overwrite kelly_live.json.  Safe to run at any time.
* Requires: 5+ settled trades (order_settled or order_settled_live events).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml  # PyYAML — already in requirements.txt via structlog dependency

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config_production import KELLY_CONFIG, STARTING_CAPITAL  # noqa: E402
from replay.engine import ReplayEngine                        # noqa: E402

# ---------------------------------------------------------------------------
# Parameter grid (mirrors sweep_kelly_and_edge.py)
# ---------------------------------------------------------------------------

_GRID: Dict[str, List[Any]] = {
    "min_edge_required": [
        Decimal("0.005"),
        Decimal("0.010"),
        Decimal("0.020"),
        Decimal("0.030"),
    ],
    "fractional_kelly": [Decimal("0.25"), Decimal("0.50")],
    "max_bet_pct":      [Decimal("2.5"),  Decimal("5.0")],
}

MIN_SETTLED_TRADES = 5   # Do not rank combos with fewer settled trades.
CONFIG_DIR = _REPO_ROOT / "config"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_combos() -> List[Dict[str, Decimal]]:
    combos = []
    for me in _GRID["min_edge_required"]:
        for fk in _GRID["fractional_kelly"]:
            for mb in _GRID["max_bet_pct"]:
                combos.append(
                    {"min_edge_required": me, "fractional_kelly": fk, "max_bet_pct": mb}
                )
    return combos


def _window_timestamps(window_days: int) -> tuple[datetime, datetime]:
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=window_days)
    return start, now


def _compute_sharpe(returns: List[float]) -> float:
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / n
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    return round(mean_r / std_r, 4) if std_r > 0 else 0.0


def _compute_max_drawdown(equity_curve: List[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 4)  # percentage


# ---------------------------------------------------------------------------
# Rolling sweep
# ---------------------------------------------------------------------------

async def _run_combo(
    combo: Dict[str, Decimal],
    log_file: str,
    from_ts: datetime,
    to_ts: datetime,
) -> Dict[str, Any]:
    """Run a single combo through ReplayEngine and return metrics dict."""
    cfg = copy.deepcopy(KELLY_CONFIG)
    cfg["min_edge"]          = combo["min_edge_required"]
    cfg["fractional_kelly"]  = combo["fractional_kelly"]
    cfg["max_bet_fraction"]  = combo["max_bet_pct"] / Decimal("100")

    engine = ReplayEngine(
        log_file=log_file,
        kelly_config=cfg,
        initial_equity=STARTING_CAPITAL,
        from_ts=from_ts,
        to_ts=to_ts,
    )
    # run() returns the full metrics dict — ReplayEngine has no separate stats() method.
    stats = await engine.run()

    settled = stats.get("settled_trades", 0) or stats.get("total_trades", 0) or 0
    wins    = stats.get("wins", 0) or 0
    win_pct = (wins / settled * 100) if settled > 0 else None

    # sharpe and max_drawdown_pct are already computed inside ReplayMetrics.to_dict();
    # equity_series is available if custom recomputation is ever needed.
    sharpe = stats.get("sharpe") or 0.0
    max_dd = stats.get("max_drawdown_pct", 0.0)
    cagr   = stats.get("cagr", None)

    return {
        "min_edge":       float(combo["min_edge_required"]),
        "fractional_kelly": float(combo["fractional_kelly"]),
        "max_bet_pct":    float(combo["max_bet_pct"]),
        "sharpe":         sharpe,
        "cagr":           cagr,
        "max_drawdown_pct": max_dd,
        "win_pct":        win_pct,
        "trade_count":    settled,
    }


async def _run_sweep(
    log_file: str,
    window_days: int,
) -> List[Dict[str, Any]]:
    from_ts, to_ts = _window_timestamps(window_days)
    combos   = _build_combos()
    results  = []

    print(f"Rolling Kelly optimizer | log={log_file} | window={window_days}d")
    print(f"  period: {from_ts.date()} → {to_ts.date()} | combos: {len(combos)}")

    for i, combo in enumerate(combos, 1):
        label = (
            f"min_edge={combo['min_edge_required']} "
            f"fk={combo['fractional_kelly']} "
            f"max_bet={combo['max_bet_pct']}"
        )
        print(f"[{i:02d}/{len(combos)}] {label} ...", end="", flush=True)
        row = await _run_combo(combo, log_file, from_ts, to_ts)
        print(f"  sharpe={row['sharpe']:.3f}  dd={row['max_drawdown_pct']:.2f}%  trades={row['trade_count']}")
        results.append(row)

    return results


# ---------------------------------------------------------------------------
# Ranking and persistence
# ---------------------------------------------------------------------------

def _pick_winner(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick best combo: most trades >= MIN_SETTLED_TRADES, ranked by Sharpe desc."""
    eligible = [r for r in results if r["trade_count"] >= MIN_SETTLED_TRADES]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (r["sharpe"], -r["max_drawdown_pct"]))


def _write_snapshot(winner: Dict[str, Any], optimized_at: str) -> Path:
    """Write YAML snapshot and kelly_live.json."""
    CONFIG_DIR.mkdir(exist_ok=True)
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    # Human-readable YAML archive
    snap_path = CONFIG_DIR / f"kelly_config_snapshot_{today}.yaml"
    snap_data = {
        "generated_at":      optimized_at,
        "window_days":       winner.get("window_days", 7),
        "best_combo": {
            "min_edge_required": winner["min_edge"],
            "fractional_kelly":  winner["fractional_kelly"],
            "max_bet_pct":       winner["max_bet_pct"],
        },
        "metrics": {
            "sharpe":          winner["sharpe"],
            "max_drawdown_pct": winner["max_drawdown_pct"],
            "win_pct":         winner["win_pct"],
            "trade_count":     winner["trade_count"],
        },
        "notes": (
            "Auto-generated by experiments/rolling_kelly_optimizer.py. "
            "Do not edit manually — next run will overwrite. "
            "The best_combo values are loaded into main.py on next restart "
            "via config/kelly_live.json."
        ),
    }
    with open(snap_path, "w") as fh:
        yaml.dump(snap_data, fh, default_flow_style=False, sort_keys=False)

    # Machine-readable JSON for main.py's existing startup loader
    live_path = CONFIG_DIR / "kelly_live.json"
    live_data = {
        "min_edge_required":  winner["min_edge"],
        "fractional_kelly":   winner["fractional_kelly"],
        "max_bet_pct":        winner["max_bet_pct"] / 100.0,  # stored as fraction in KELLY_CONFIG
        "sharpe":             winner["sharpe"],
        "trade_count":        winner["trade_count"],
        "optimized_at":       optimized_at,
    }
    with open(live_path, "w") as fh:
        json.dump(live_data, fh, indent=2)

    return snap_path


def _print_table(results: List[Dict[str, Any]], winner: Optional[Dict[str, Any]]) -> None:
    header = f"{'min_edge':>8}  {'frac_k':>6}  {'max_bet%':>8}  {'sharpe':>7}  {'dd%':>6}  {'win%':>6}  {'trades':>6}"
    sep    = "-" * len(header)
    print(f"\n{'='*70}")
    print(f"  Rolling window results (sorted by Sharpe desc)")
    print(f"{'='*70}")
    print(header)
    print(sep)
    for r in sorted(results, key=lambda x: x["sharpe"], reverse=True):
        tag = " <<< WINNER" if winner and (
            r["min_edge"] == winner["min_edge"]
            and r["fractional_kelly"] == winner["fractional_kelly"]
            and r["max_bet_pct"] == winner["max_bet_pct"]
        ) else ""
        win = f"{r['win_pct']:.1f}" if r["win_pct"] is not None else "  N/A"
        cagr = f"{r['cagr']:.2%}" if r["cagr"] is not None else "  N/A"
        print(
            f"{r['min_edge']:>8.3f}  {r['fractional_kelly']:>6.2f}  {r['max_bet_pct']:>8.1f}"
            f"  {r['sharpe']:>7.4f}  {r['max_drawdown_pct']:>6.2f}  {win:>6}  {r['trade_count']:>6}{tag}"
        )
    print(sep)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Rolling Kelly parameter optimizer")
    parser.add_argument("--log",    default="logs/production.log", help="Path to production log")
    parser.add_argument("--window", type=int, default=7,           help="Rolling window in days (default 7)")
    args = parser.parse_args()

    results  = asyncio.run(_run_sweep(args.log, args.window))
    winner   = _pick_winner(results)
    now_str  = datetime.now(tz=timezone.utc).isoformat()

    _print_table(results, winner)

    if winner is None:
        print(
            f"\n[OPTIMIZER] No combo has >= {MIN_SETTLED_TRADES} settled trades in the window."
            f"\n            kelly_live.json NOT updated — current config remains active."
            f"\n            Run again after more trades settle."
        )
        return

    winner["window_days"] = args.window
    snap_path = _write_snapshot(winner, now_str)
    print(f"\n[OPTIMIZER] Winner: min_edge={winner['min_edge']:.3f} fk={winner['fractional_kelly']:.2f} max_bet={winner['max_bet_pct']:.1f}%")
    print(f"            Sharpe={winner['sharpe']:.4f}  DD={winner['max_drawdown_pct']:.2f}%  trades={winner['trade_count']}")
    print(f"            Snapshot: {snap_path}")
    print(f"            Live config: {CONFIG_DIR / 'kelly_live.json'}")
    print(f"            Restart the bot to apply the new config.")


if __name__ == "__main__":
    main()
