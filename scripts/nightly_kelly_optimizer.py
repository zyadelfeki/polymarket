"""
Nightly Kelly parameter optimizer.

Run nightly at 02:00 UTC via Windows Task Scheduler (see DEPLOYMENT.md).
Reads the last 30 days of settled trades from the ledger, sweeps 32
parameter combinations, picks the highest-Sharpe config that satisfies
drawdown + win-rate constraints, and writes it to config/kelly_live.json.

main.py loads config/kelly_live.json at startup (if present) and overrides
the hardcoded KELLY_CONFIG / CHARLIE_CONFIG defaults.

Usage (manual run):
    python scripts/nightly_kelly_optimizer.py

Schedule (PowerShell — run once to install):
    $action = New-ScheduledTaskAction -Execute "python" `
        -Argument "scripts/nightly_kelly_optimizer.py" `
        -WorkingDirectory "C:\\Users\\zyade\\polymarket"
    $trigger = New-ScheduledTaskTrigger -Daily -At "02:00"
    Register-ScheduledTask -TaskName "PolyKellyOptimizer" `
        -Action $action -Trigger $trigger -RunLevel Highest
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Ensure project root is on path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Parameter grid — 4 × 4 × 2 = 32 combinations
# ---------------------------------------------------------------------------
PARAM_GRID = {
    "min_edge_required": [0.03, 0.05, 0.07, 0.10],
    "fractional_kelly":  [0.25, 0.35, 0.50, 0.65],
    "max_bet_pct":       [0.03, 0.05],
}

# Constraints
MIN_TRADES = 20        # don't optimize without meaningful sample
MAX_DRAWDOWN = 0.20    # reject configs that produce >20% peak-to-trough
MIN_WIN_RATE = 0.45    # reject configs with <45% win rate


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def _simulate(trades: list[dict], params: dict) -> tuple[float, float, float]:
    """
    Replay trades with given params.
    Returns (annualised_sharpe, max_drawdown, win_rate).
    """
    min_edge     = params["min_edge_required"]
    frac_kelly   = params["fractional_kelly"]
    max_bet_pct  = params["max_bet_pct"]

    equity = 1.0   # normalised
    equity_history = [equity]
    peak = equity
    wins = 0
    losses = 0
    returns: list[float] = []

    for t in trades:
        edge: float = float(t.get("edge", 0) or 0)
        pnl:  float = float(t.get("pnl",  0) or 0)
        size: float = float(t.get("size", 0) or 0)

        if edge < min_edge or size <= 0:
            continue

        # Scale bet by fractional Kelly ratio and max_bet_pct cap
        bet_fraction = min(frac_kelly * edge, max_bet_pct)
        bet = equity * bet_fraction

        # Actual return on this bet (proportional to recorded PnL)
        if size > 0:
            trade_return = (pnl / size) * bet
        else:
            trade_return = 0.0

        ret_pct = trade_return / equity if equity > 0 else 0.0
        equity += trade_return
        equity_history.append(equity)
        returns.append(ret_pct)

        if trade_return >= 0:
            wins += 1
        else:
            losses += 1

        if equity > peak:
            peak = equity

    total_trades = wins + losses
    if total_trades == 0:
        return -999.0, 1.0, 0.0

    win_rate = wins / total_trades

    # Max drawdown
    peak_seen = equity_history[0]
    max_dd = 0.0
    for eq in equity_history:
        if eq > peak_seen:
            peak_seen = eq
        dd = (peak_seen - eq) / peak_seen if peak_seen > 0 else 0.0
        max_dd = max(max_dd, dd)

    # Annualised Sharpe (assume 252 trading days, each trade ~1 hour apart)
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        std_r  = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1))
        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    return sharpe, max_dd, win_rate


# ---------------------------------------------------------------------------
# DB read — works directly with SQLite (no async needed for a scheduled script)
# ---------------------------------------------------------------------------

def _load_settled_trades(db_path: str, days: int = 30) -> list[dict]:
    """Load SETTLED orders from the last N days."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = datetime.now(timezone.utc)
        cutoff_str = cutoff.strftime(f"%Y-%m-%d")
        # SQLite date arithmetic: opened_at >= date('now', '-{days} days')
        rows = conn.execute(
            f"""
            SELECT order_id, market_id, side, size, price, pnl,
                   charlie_p_win, charlie_regime, opened_at, closed_at,
                   notes
            FROM order_tracking
            WHERE order_state = 'SETTLED'
              AND opened_at >= date('now', '-{days} days')
            ORDER BY opened_at ASC
            """
        ).fetchall()

        trades = []
        for r in rows:
            # Parse edge from notes field ("charlie_signal ... edge=0.073 ...")
            notes = r["notes"] or ""
            edge  = 0.0
            for part in notes.split():
                if part.startswith("edge="):
                    try:
                        edge = float(part.split("=")[1])
                    except ValueError:
                        pass

            trades.append({
                "order_id":  r["order_id"],
                "market_id": r["market_id"],
                "side":      r["side"],
                "size":      r["size"],
                "price":     r["price"],
                "pnl":       r["pnl"],
                "edge":      edge,
                "opened_at": r["opened_at"],
            })
        return trades
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_nightly(db_path: str = "data/trading.db", days: int = 30) -> None:
    config_dir = _ROOT / "config"
    config_dir.mkdir(exist_ok=True)
    output_path = config_dir / "kelly_live.json"

    print(f"[KellyOptimizer] Loading last {days} days of settled trades from {db_path}…")
    trades = _load_settled_trades(db_path, days)
    print(f"[KellyOptimizer] Found {len(trades)} settled trades.")

    if len(trades) < MIN_TRADES:
        print(
            f"[KellyOptimizer] Insufficient trades ({len(trades)} < {MIN_TRADES}). "
            "Skipping optimization — keeping existing config."
        )
        return

    best_config = None
    best_sharpe = -999.0

    combos = list(itertools.product(*PARAM_GRID.values()))
    print(f"[KellyOptimizer] Sweeping {len(combos)} parameter combinations…")

    for combo in combos:
        params = dict(zip(PARAM_GRID.keys(), combo))
        sharpe, max_dd, win_rate = _simulate(trades, params)

        if sharpe > best_sharpe and max_dd < MAX_DRAWDOWN and win_rate > MIN_WIN_RATE:
            best_sharpe = sharpe
            best_config = {
                **params,
                "sharpe":      round(sharpe, 4),
                "max_drawdown": round(max_dd, 4),
                "win_rate":    round(win_rate, 4),
                "trade_count": len(trades),
                "days":        days,
                "optimized_at": datetime.utcnow().isoformat() + "Z",
            }

    if best_config is None:
        print(
            "[KellyOptimizer] No parameter combination passed constraints "
            f"(max_drawdown<{MAX_DRAWDOWN}, win_rate>{MIN_WIN_RATE}). "
            "Keeping existing config."
        )
        return

    output_path.write_text(json.dumps(best_config, indent=2))
    print(
        f"[KellyOptimizer] Wrote new config to {output_path}:\n"
        f"  min_edge_required = {best_config['min_edge_required']}\n"
        f"  fractional_kelly  = {best_config['fractional_kelly']}\n"
        f"  max_bet_pct       = {best_config['max_bet_pct']}\n"
        f"  sharpe            = {best_config['sharpe']}\n"
        f"  win_rate          = {best_config['win_rate']:.1%}\n"
        f"  max_drawdown      = {best_config['max_drawdown']:.1%}\n"
        f"  trade_count       = {best_config['trade_count']}\n"
    )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Nightly Kelly parameter optimizer")
    p.add_argument("--db",   default="data/trading.db", help="Path to SQLite DB")
    p.add_argument("--days", type=int, default=30,       help="Lookback window in days")
    args = p.parse_args()
    run_nightly(db_path=args.db, days=args.days)
