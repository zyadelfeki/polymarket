"""
Diagnostic: last 20 settled trades with per-market win/loss breakdown.
Run: python scripts/last20_trades.py
"""
import sqlite3
from pathlib import Path
from collections import defaultdict

DB = Path(__file__).resolve().parent.parent / "data" / "trading.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    """
    SELECT market_id, order_state, pnl, charlie_p_win, charlie_conf,
           price, size, outcome, mode, strategy,
           opened_at, COALESCE(closed_at, opened_at) AS sort_key
    FROM order_tracking
    WHERE order_state IN ('SETTLED', 'RESOLVED')
    ORDER BY sort_key DESC
    LIMIT 20
    """
).fetchall()
conn.close()

if not rows:
    print("No settled/resolved trades found in order_tracking.")
    raise SystemExit(0)

print(f"\n{'#':>3}  {'W/L':>4}  {'PnL':>8}  {'p_win':>6}  {'conf':>6}  {'price':>6}  {'size':>7}  {'out':>4}  {'closed':>16}  market_id")
print("-" * 110)

wins = 0
per_market: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
for i, r in enumerate(rows, 1):
    pnl = float(r["pnl"]) if r["pnl"] is not None else None
    won = pnl is not None and pnl > 0
    if pnl is not None:
        wins += int(won)
    sign = "WIN" if won else ("LOSS" if pnl is not None and pnl < 0 else "PUSH")
    p_win = f"{float(r['charlie_p_win']):.3f}" if r["charlie_p_win"] is not None else "   n/a"
    conf  = f"{float(r['charlie_conf']):.3f}" if r["charlie_conf"] is not None else "   n/a"
    price = f"{float(r['price']):.3f}" if r["price"] is not None else "   n/a"
    size  = f"{float(r['size']):.2f}" if r["size"] is not None else "    n/a"
    out   = str(r["outcome"] or "")[:4]
    closed = str(r["sort_key"])[:16]
    mkt = r["market_id"] or "unknown"
    print(f"{i:>3}  {sign:>4}  {pnl:>8.4f}  {p_win:>6}  {conf:>6}  {price:>6}  {size:>7}  {out:>4}  {closed:>16}  {mkt}")

    if pnl is not None:
        per_market[mkt]["wins" if won else "losses"] += 1
        per_market[mkt]["pnl"] += pnl

total = len(rows)
print(f"\nWin rate (last 20): {wins}/{total} = {wins/total:.1%}")

# Per-market summary of the losers
losers = [(mkt, d) for mkt, d in per_market.items() if d["losses"] > 0]
losers.sort(key=lambda x: x[1]["pnl"])

if losers:
    print(f"\n--- Market breakdown (losers) ---")
    print(f"{'market_id':<20}  {'W':>3}  {'L':>3}  {'total_pnl':>10}")
    for mkt, d in losers:
        print(f"{mkt:<20}  {d['wins']:>3}  {d['losses']:>3}  {d['pnl']:>10.4f}")
