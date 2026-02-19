"""
tests/smoke_replay.py
=====================
End-to-end smoke test for the replay harness.

Runs the ReplayEngine against a tiny synthetic log
(tests/fixtures/smoke_replay.log) and asserts:

  1. No Python exceptions surface.
  2. Exact trade count matches the synthetic log.
  3. Final equity is positive and plausible.
  4. Key metrics exist and are sensible types.
  5. No KeyError / TypeError from ledger row access
     (which would indicate the as_dict regression is still present).

Usage
-----
    python tests/smoke_replay.py

Exit 0 = all assertions passed.
Exit 1 = one or more assertions failed.
"""
from __future__ import annotations

import asyncio
import sys
import os
from decimal import Decimal
from pathlib import Path

# Bootstrap path so script can be run from any cwd.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from replay.engine import ReplayEngine  # noqa: E402

_FIXTURE_LOG = _REPO / "tests" / "fixtures" / "smoke_replay.log"

# Known ground truth from the synthetic log:
#   8 opportunity events  → 8 attempted trades
#   8 settlement events   → 8 settlements
#   PnL = 0.82 + 1.15 - 0.28 - 0.22 + 0.61 + 1.48 + 0.39 - 0.19 = 3.76
#   5 wins / 8 settlements → win_rate ≈ 0.625
#
#   Using $100 initial equity so Kelly sizes ($5 max) exceed the
#   $1 min_position_size default and all 8 trades actually execute.
#   With prod STARTING_CAPITAL ($13.98) the max bet is ~$0.70, below
#   min_pos, so 0 trades would fire — useless for a smoke test.
_INITIAL_EQUITY = Decimal("100.00")
_EXPECTED_TRADES = 8
_EXPECTED_SETTLEMENTS = 8
_EXPECTED_WINS = 5
_EXPECTED_PNL_APPROX = 3.76
_EXPECTED_WIN_RATE_APPROX = 0.625


def _check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))
    return condition


async def run_smoke() -> bool:
    print(f"\n== Replay smoke test ==")
    print(f"   fixture: {_FIXTURE_LOG}")
    print(f"   initial equity: ${_INITIAL_EQUITY} (smoke-test override)")

    if not _FIXTURE_LOG.exists():
        print(f"\n  FAIL  fixture not found: {_FIXTURE_LOG}")
        return False

    engine = ReplayEngine(
        log_file=str(_FIXTURE_LOG),
        # No date filter — consume the entire fixture.
        from_ts=None,
        to_ts=None,
        # Default production config.
        kelly_config=None,
        initial_equity=_INITIAL_EQUITY,
        slippage_bps=0.0,
        # Never touch production baseline during smoke tests.
        baseline_path=None,
    )

    results = await engine.run()

    print(f"\n--- Results ---")
    for k in (
        "total_trades", "settled_trades", "wins", "win_rate",
        "total_pnl", "final_equity", "max_drawdown_pct", "cagr", "sharpe",
        "days_covered", "auto_blocks",
    ):
        print(f"  {k:30s} = {results.get(k)}")

    print(f"\n--- Summary ---")
    print(results.get("summary", "(no summary)"))

    print(f"\n--- Assertions ---")
    ok = True

    # 1. No exception (reaching here means no crash)
    ok &= _check("no exception during engine.run()", True)

    # 2. Trade count: all 8 opportunities must pass Kelly filter
    #    (all have edge > 0.02 min_edge_required)
    trades = results.get("total_trades", -1)
    ok &= _check(
        f"total_trades == {_EXPECTED_TRADES}",
        trades == _EXPECTED_TRADES,
        f"got {trades}",
    )

    # 3. Settlement count
    settled = results.get("settled_trades", -1)
    ok &= _check(
        f"settled_trades == {_EXPECTED_SETTLEMENTS}",
        settled == _EXPECTED_SETTLEMENTS,
        f"got {settled}",
    )

    # 4. Wins
    wins = results.get("wins", -1)
    ok &= _check(
        f"wins == {_EXPECTED_WINS}",
        wins == _EXPECTED_WINS,
        f"got {wins}",
    )

    # 5. Win rate
    wr = results.get("win_rate")
    ok &= _check(
        f"win_rate ≈ {_EXPECTED_WIN_RATE_APPROX:.3f}",
        wr is not None and abs(wr - _EXPECTED_WIN_RATE_APPROX) < 0.005,
        f"got {wr}",
    )

    # 6. PnL should be close to expected (small deviation allowed because
    #    Kelly may size below cap meaning we don't reject any trade, just
    #    the settlement PnL is taken verbatim from the log)
    pnl = results.get("total_pnl", 0.0) or 0.0
    ok &= _check(
        f"total_pnl ≈ {_EXPECTED_PNL_APPROX:.2f} (within $1)",
        abs(pnl - _EXPECTED_PNL_APPROX) < 1.0,
        f"got {pnl}",
    )

    # 7. Final equity is positive
    final_eq = results.get("final_equity", 0.0) or 0.0
    ok &= _check(
        "final_equity > 0",
        final_eq > 0,
        f"got {final_eq}",
    )

    # 8. Days covered (based on equity_series first→last settlement
    #    span: Jan-16 → Jan-22 ≈ 5.7 days; log starts Jan-15 but
    #    equity_series only records after settlements)
    days = results.get("days_covered", 0.0) or 0.0
    ok &= _check(
        "days_covered ≥ 5",
        days >= 5.0,
        f"got {days}",
    )

    # 9. max_drawdown_pct is a non-negative float
    dd = results.get("max_drawdown_pct")
    ok &= _check(
        "max_drawdown_pct is non-negative float",
        isinstance(dd, (int, float)) and dd >= 0,
        f"got {dd!r}",
    )

    # 10. No auto-blocks (all markets have positive equity; none hit slippage
    #     guard since slippage_bps=0)
    ab = results.get("auto_blocks", -1)
    ok &= _check(
        "auto_blocks == 0",
        ab == 0,
        f"got {ab}",
    )

    print()
    if ok:
        print("==> ALL ASSERTIONS PASSED\n")
    else:
        print("==> SOME ASSERTIONS FAILED — review output above\n")

    return ok


def main() -> None:
    passed = asyncio.run(run_smoke())
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
