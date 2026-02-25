#!/usr/bin/env python3
"""
scripts/pnl_report.py
======================
Standalone PnL attribution report grouped by market.

Usage:
    python scripts/pnl_report.py

Queries the live SQLite ledger (same DB as main.py) and prints a formatted
table showing realized PnL, win rate, average edge, and average Charlie p_win
for every SETTLED market.

Useful for:
  - Identifying which markets produce consistent profits vs noise.
  - Validating that Charlie's p_win correlates with actual win rates.
  - Quick post-session review alongside check_session.py.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from database.ledger_async import AsyncLedger  # noqa: E402


async def main() -> None:
    ledger = AsyncLedger()
    await ledger.pool.initialize()

    rows = await ledger.get_pnl_by_market()
    equity = await ledger.get_equity()

    await ledger.close()

    total_trades  = sum(r["trade_count"] for r in rows)
    total_wins    = sum(r["win_count"]   for r in rows)
    total_pnl_sum = sum(float(r["total_pnl"]) for r in rows)

    print("=" * 80)
    print("  PnL Attribution by Market (SETTLED orders only)")
    print("=" * 80)

    if not rows:
        print("  No settled orders in the ledger yet.")
        print(f"  Current equity: {float(equity):.4f} USDC")
        return

    hdr = (
        f"  {'market_id':>14}  {'trades':>6}  {'wins':>5}  {'win%':>6}"
        f"  {'total_pnl':>10}  {'avg_edge':>9}  {'avg_p_win':>9}"
    )
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)

    for r in rows:
        win_pct = f"{100 * r['win_count'] / r['trade_count']:.0f}%" if r["trade_count"] > 0 else "  N/A"
        avg_e   = f"{r['avg_edge']:.4f}"  if r["avg_edge"]  is not None else "     N/A"
        avg_pw  = f"{r['avg_p_win']:.4f}" if r["avg_p_win"] is not None else "     N/A"
        print(
            f"  {str(r['market_id']):>14}  {r['trade_count']:>6}  {r['win_count']:>5}"
            f"  {win_pct:>6}  {float(r['total_pnl']):>+10.4f}  {avg_e:>9}  {avg_pw:>9}"
        )

    print(sep)
    overall_win_pct = f"{100 * total_wins / total_trades:.1f}%" if total_trades > 0 else "N/A"
    print(
        f"  {'TOTAL':>14}  {total_trades:>6}  {total_wins:>5}"
        f"  {overall_win_pct:>6}  {total_pnl_sum:>+10.4f}"
    )
    print()
    print(f"  Current equity: {float(equity):.4f} USDC")
    print("=" * 80)

    # Signal quality check: does avg_p_win predict win rate?
    calibrated = [r for r in rows if r["avg_p_win"] is not None and r["trade_count"] >= 3]
    if calibrated:
        print()
        print("  Signal calibration check (markets with >= 3 settled trades):")
        for r in calibrated:
            actual_win = r["win_count"] / r["trade_count"] if r["trade_count"] > 0 else 0.0
            gap = actual_win - r["avg_p_win"]
            direction = "overconfident" if gap < -0.05 else ("underconfident" if gap > 0.05 else "calibrated")
            print(
                f"    {str(r['market_id']):>14}  avg_p_win={r['avg_p_win']:.3f}"
                f"  actual_win%={actual_win:.3f}  gap={gap:+.3f}  [{direction}]"
            )


if __name__ == "__main__":
    asyncio.run(main())
