"""
experiments/sweep_kelly_and_edge.py
=====================================
Grid-search over three Kelly/risk parameters on a fixed historical log window,
ranking each combo by Sharpe ratio and max drawdown.

Usage
-----
    python experiments/sweep_kelly_and_edge.py \\
        --log-file bot_production.log \\
        --from 2026-01-01T00:00:00Z \\
        --to   2026-02-01T00:00:00Z \\
        --output results/kelly_sweep.csv

Results
-------
* Writes every combination's metrics to ``--output`` (CSV).
* Prints a ranked table: top-10 by Sharpe, then top-10 with
  ``max_drawdown_pct <= 5 %`` sorted by ascending drawdown.

Parameter grid (16 combinations)
---------------------------------
| Parameter           | Values                             |
|---------------------|------------------------------------|
| min_edge_required   | 0.005, 0.010, 0.020, 0.030         |
| fractional_kelly    | 0.25, 0.50                         |
| max_bet_pct         | 2.5, 5.0                           |

Design decisions
----------------
* ``ReplayEngine`` already handles all state — we only swap ``kelly_config``.
* We re-load the log events once per combo but ``load_log_events`` is O(n)
  and cheap; no premature optimisation.
* Sweep is sequential (one ``asyncio.run`` wrapper) to keep resource pressure
  low and output deterministic.  Parallel would not be meaningful at this scale.
* ``Decimal`` is used when patching the config to stay consistent with the
  production code path inside ``KellySizer``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import copy
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure the repo root is on sys.path when the script is run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config_production import KELLY_CONFIG, STARTING_CAPITAL  # noqa: E402
from replay.engine import ReplayEngine  # noqa: E402


# ---------------------------------------------------------------------------
# Parameter grid
# ---------------------------------------------------------------------------

_GRID: Dict[str, List[Any]] = {
    "min_edge_required": [
        Decimal("0.005"),
        Decimal("0.010"),
        Decimal("0.020"),
        Decimal("0.030"),
    ],
    "fractional_kelly": [Decimal("0.25"), Decimal("0.50")],
    "max_bet_pct": [Decimal("2.5"), Decimal("5.0")],
}

# Metrics we capture for ranking.  Mirrors ReplayMetrics.to_dict() + ReplayEngine
# derived keys.  NOTE: the correct key is "settled_trades" not "total_settlements".
_METRICS = [
    "total_pnl",
    "cagr",
    "sharpe",
    "max_drawdown_pct",
    "win_rate",
    "total_trades",
    "settled_trades",      # fixed: was incorrectly "total_settlements"
    "open_trades",         # total_trades - settled_trades
    "deployed_capital",    # cash in open positions (initial_equity - replay_equity)
    "final_replay_equity", # liquid capital remaining after deployments
    "auto_blocks",
    "days_covered",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 UTC timestamp (trailing Z optional)."""
    ts = ts.rstrip("Z")
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


def _to_float(val: Any, default: float = 0.0) -> float:
    """Safely coerce a CSV field to float.

    Handles Python None, the serialised string 'None', empty string,
    and genuine numeric strings/floats.  Falls back to *default* on any
    conversion failure so sort keys and filters never crash.
    """
    if val is None or str(val).strip().lower() in ("", "none", "nan"):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _iter_grid() -> List[Dict[str, Decimal]]:
    """
    Return the Cartesian product of _GRID as a list of parameter dicts.
    Deterministically ordered: min_edge outer, fractional_kelly middle,
    max_bet_pct inner.
    """
    combos: List[Dict[str, Decimal]] = []
    for min_edge in _GRID["min_edge_required"]:
        for frac_k in _GRID["fractional_kelly"]:
            for max_bet in _GRID["max_bet_pct"]:
                combos.append(
                    {
                        "min_edge_required": min_edge,
                        "fractional_kelly": frac_k,
                        "max_bet_pct": max_bet,
                    }
                )
    return combos


def _build_kelly_config(overrides: Dict[str, Decimal]) -> Dict[str, Any]:
    """
    Deep-copy KELLY_CONFIG and apply the supplied overrides.
    All values are kept as Decimal to stay consistent with KellySizer.
    """
    cfg = copy.deepcopy(dict(KELLY_CONFIG))
    cfg.update(overrides)
    return cfg


def _format_row(combo: Dict[str, Decimal], metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten combo params + metric results into a single CSV row dict."""
    row: Dict[str, Any] = {
        "min_edge_required": str(combo["min_edge_required"]),
        "fractional_kelly": str(combo["fractional_kelly"]),
        "max_bet_pct": str(combo["max_bet_pct"]),
    }
    for key in _METRICS:
        val = metrics.get(key, "")
        row[key] = f"{val:.4f}" if isinstance(val, float) else str(val)
    return row


def _print_table(
    title: str,
    rows: List[Dict[str, Any]],
    sort_key: str,
    reverse: bool = True,
    limit: int = 10,
) -> None:
    """Print a ranked subset of rows to stdout in a fixed-width table."""
    sorted_rows = sorted(
        rows,
        key=lambda r: _to_float(r.get(sort_key)),
        reverse=reverse,
    )[:limit]

    print(f"\n{'=' * 72}")
    print(f"  {title}")
    print(f"{'=' * 72}")
    header_cols = [
        "min_edge", "frac_k", "max_bet%",
        "sharpe", "cagr", "dd%", "win%", "trades",
    ]
    col_widths = [9, 7, 9, 8, 8, 7, 6, 7]
    header = "  ".join(h.ljust(w) for h, w in zip(header_cols, col_widths))
    print(header)
    print("-" * 72)
    for r in sorted_rows:
        parts = [
            str(r["min_edge_required"]).ljust(col_widths[0]),
            str(r["fractional_kelly"]).ljust(col_widths[1]),
            str(r["max_bet_pct"]).ljust(col_widths[2]),
            str(r.get("sharpe", "")).ljust(col_widths[3]),
            str(r.get("cagr", "")).ljust(col_widths[4]),
            str(r.get("max_drawdown_pct", "")).ljust(col_widths[5]),
            str(r.get("win_rate", "")).ljust(col_widths[6]),
            str(r.get("total_trades", "")).ljust(col_widths[7]),
        ]
        print("  ".join(parts))


# ---------------------------------------------------------------------------
# Mark-to-market Sharpe helper (used when no order_settled events exist)
# ---------------------------------------------------------------------------


def _compute_mtm_sharpe(
    log_file: str,
    from_ts: Optional[datetime],
    to_ts: Optional[datetime],
) -> float:
    """
    Compute a mark-to-market Sharpe for open positions when no settlements exist.

    For each market in the log window:
      - entry = market_price from the FIRST arbitrage_opportunity_detected event
      - mark  = market_price from the LAST  arbitrage_opportunity_detected event
      - unrealized_return = (mark - entry) / entry

    When mark == entry (no observed price movement), return = 0.
    Returns 0.0 when variance of returns is zero (flat positions — honest).
    Returns 0.0 when fewer than 2 distinct markets are observed.

    Why: Sharpe = None means "undefined" and corrupts sort/display in the table.
    Sharpe = 0.0 means "flat open position, no data yet" — honest and sortable.
    Once markets resolve and order_settled events appear, real returns take over.
    """
    import json as _json  # avoid shadowing module-level name in caller scope

    try:
        raw_lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0

    first_price: Dict[str, float] = {}
    last_price: Dict[str, float] = {}

    for raw_line in raw_lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            ev = _json.loads(raw_line)
        except ValueError:
            continue
        if ev.get("event") != "arbitrage_opportunity_detected":
            continue
        # Time-window filter — matches the engine's own filtering
        ts_raw = ev.get("timestamp")
        if ts_raw and (from_ts or to_ts):
            try:
                ts = datetime.fromisoformat(ts_raw.rstrip("Z")).replace(tzinfo=timezone.utc)
                if from_ts and ts < from_ts:
                    continue
                if to_ts and ts > to_ts:
                    continue
            except ValueError:
                pass
        mid = ev.get("market_id", "")
        price_raw = ev.get("market_price")
        if not mid or price_raw is None:
            continue
        try:
            price_f = float(price_raw)
        except (TypeError, ValueError):
            continue
        if mid not in first_price:
            first_price[mid] = price_f
        last_price[mid] = price_f

    if len(first_price) < 2:
        # Only 0 or 1 market observed — not enough for a meaningful Sharpe.
        return 0.0

    returns = [
        (last_price[mid] - entry) / entry if entry > 0 else 0.0
        for mid, entry in first_price.items()
    ]
    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    std_r = math.sqrt(variance) if variance > 0 else 0.0
    # If std = 0, all returns are equal (likely all 0 — flat, no movement).
    # Return 0.0 rather than None so the table is sortable and honest.
    return round(mean_r / std_r, 4) if std_r > 0 else 0.0


# ---------------------------------------------------------------------------
# Core sweep
# ---------------------------------------------------------------------------

async def _run_sweep(
    log_file: str,
    from_ts: Optional[datetime],
    to_ts: Optional[datetime],
    output_path: Path,
) -> List[Dict[str, Any]]:
    """
    Iterate over all parameter combos, run each through ReplayEngine in memory,
    and collect results.  Writes CSV on completion and returns the raw rows.
    """
    combos = _iter_grid()
    total = len(combos)
    rows: List[Dict[str, Any]] = []

    # CSV fieldnames: params first, then metrics, then error column.
    fieldnames = (
        ["min_edge_required", "fractional_kelly", "max_bet_pct"]
        + _METRICS
        + ["error"]
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Sweep: {total} combinations | log={log_file}")
    if from_ts:
        print(f"  from: {from_ts.isoformat()}")
    if to_ts:
        print(f"  to:   {to_ts.isoformat()}")
    print(f"  output: {output_path}\n")

    with open(output_path, "w", newline="", encoding="utf-8") as csv_fh:
        writer = csv.DictWriter(csv_fh, fieldnames=fieldnames)
        writer.writeheader()

        for idx, combo in enumerate(combos, start=1):
            tag = (
                f"min_edge={combo['min_edge_required']} "
                f"fk={combo['fractional_kelly']} "
                f"max_bet={combo['max_bet_pct']}"
            )
            print(f"[{idx:02d}/{total}] {tag} ...", end="", flush=True)

            kelly_cfg = _build_kelly_config(combo)
            engine = ReplayEngine(
                log_file=log_file,
                from_ts=from_ts,
                to_ts=to_ts,
                kelly_config=kelly_cfg,
                initial_equity=STARTING_CAPITAL,
                # No slippage — isolate parameter effects cleanly.
                slippage_bps=0.0,
                # No baseline for individual sweep runs; regression check is
                # only meaningful for production replays.
                baseline_path=None,
            )

            error_msg = ""
            metrics: Dict[str, Any] = {}
            try:
                results = await engine.run()
                metrics = results  # to_dict() already merged into results
            except Exception as exc:  # noqa: BLE001
                error_msg = f"{type(exc).__name__}: {exc}"
                print(f" ERROR — {error_msg}")
            else:
                # Mark-to-market: if sharpe is None but open trades exist, compute
                # unrealized returns from last observed prices in the log.
                # When mark == entry (no price movement), returns = 0 → Sharpe = 0.0.
                # This is honest and prevents None from corrupting the final table.
                if metrics.get("sharpe") is None:
                    open_count = (
                        metrics.get("total_trades", 0)
                        - metrics.get("settled_trades", 0)
                    )
                    if open_count > 0:
                        metrics["sharpe"] = _compute_mtm_sharpe(
                            log_file, from_ts, to_ts
                        )
                    else:
                        # No open trades, no settled trades — genuinely no data.
                        metrics["sharpe"] = 0.0

                sharpe = metrics.get("sharpe", 0.0) or 0.0
                dd = metrics.get("max_drawdown_pct", 0.0) or 0.0
                trades = metrics.get("total_trades", 0)
                print(f" sharpe={sharpe:.3f}  dd={dd:.2f}%  trades={trades}")

            row = _format_row(combo, metrics)
            row["error"] = error_msg
            rows.append(row)
            writer.writerow(row)
            csv_fh.flush()  # persist incrementally in case of crash mid-sweep

    return rows


def _print_ranked_tables(rows: List[Dict[str, Any]]) -> None:
    """Print two ranked summary tables: top-10 by Sharpe, top-10 low-drawdown."""
    clean_rows = [r for r in rows if not r.get("error")]

    _print_table(
        "Top-10 by Sharpe ratio (descending)",
        clean_rows,
        sort_key="sharpe",
        reverse=True,
        limit=10,
    )

    low_dd = [
        r for r in clean_rows
        if _to_float(r.get("max_drawdown_pct"), default=99.0) <= 5.0
    ]
    if low_dd:
        _print_table(
            "Top-10 with max_drawdown_pct ≤ 5 % (ascending drawdown)",
            low_dd,
            sort_key="max_drawdown_pct",
            reverse=False,
            limit=10,
        )
    else:
        print("\n  No combos achieved max_drawdown_pct ≤ 5 %.\n")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep Kelly/edge parameters over a fixed historical log window "
            "and rank by Sharpe ratio and drawdown."
        )
    )
    parser.add_argument(
        "--log-file",
        default="bot_production.log",
        help="Path to structlog JSON-lines log file (default: bot_production.log)",
    )
    parser.add_argument(
        "--from",
        dest="from_ts",
        default=None,
        metavar="ISO8601",
        help="Start of sweep window, e.g. 2026-01-01T00:00:00Z",
    )
    parser.add_argument(
        "--to",
        dest="to_ts",
        default=None,
        metavar="ISO8601",
        help="End of sweep window, e.g. 2026-02-01T00:00:00Z",
    )
    parser.add_argument(
        "--output",
        default="results/kelly_sweep.csv",
        help="Path for the output CSV (default: results/kelly_sweep.csv)",
    )
    args = parser.parse_args()

    from_ts = _parse_iso(args.from_ts) if args.from_ts else None
    to_ts = _parse_iso(args.to_ts) if args.to_ts else None
    output_path = Path(args.output)

    rows = asyncio.run(
        _run_sweep(
            log_file=args.log_file,
            from_ts=from_ts,
            to_ts=to_ts,
            output_path=output_path,
        )
    )

    _print_ranked_tables(rows)
    print(f"\nFull results written to: {output_path}\n")


if __name__ == "__main__":
    main()
