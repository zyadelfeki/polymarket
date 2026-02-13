import argparse
import asyncio
from decimal import Decimal
from typing import Dict, Tuple

from config_production import STARTING_CAPITAL
from database.ledger_async import AsyncLedger

DEFAULT_DB_PATH = "data/trading.db"


def _parse_period_to_hours(period: str) -> int:
    lower = period.strip().lower()
    if lower.endswith("days"):
        return int(lower.replace("days", "").strip()) * 24
    if lower.endswith("day"):
        return int(lower.replace("day", "").strip()) * 24
    if lower.endswith("hours"):
        return int(lower.replace("hours", "").strip())
    if lower.endswith("hour"):
        return int(lower.replace("hour", "").strip())
    return int(lower)


async def _fetch_trade_metrics(ledger: AsyncLedger, hours: int) -> Dict[str, Decimal | int]:
    interval = f"-{hours} hours"

    trades_row = await ledger.execute(
        """
        SELECT COUNT(*)
        FROM transactions
        WHERE transaction_type = 'TRADE'
          AND timestamp >= datetime('now', ?)
        """,
        (interval,),
        fetch_one=True,
    )
    total_trades = int(trades_row[0]) if trades_row else 0

    pnl_row = await ledger.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(realized_pnl), 0),
            COALESCE(AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END), 0),
            COALESCE(AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END), 0),
            COALESCE(MIN(unrealized_pnl + realized_pnl), 0)
        FROM positions
        WHERE (exit_timestamp IS NOT NULL AND exit_timestamp >= datetime('now', ?))
           OR (entry_timestamp >= datetime('now', ?))
        """,
        (interval, interval),
        fetch_one=True,
    )

    if pnl_row:
        winning = int(pnl_row[0] or 0)
        losing = int(pnl_row[1] or 0)
        realized_pnl = Decimal(str(pnl_row[2] or "0"))
        avg_win = Decimal(str(pnl_row[3] or "0"))
        avg_loss = Decimal(str(pnl_row[4] or "0"))
        min_running = Decimal(str(pnl_row[5] or "0"))
    else:
        winning = losing = 0
        realized_pnl = avg_win = avg_loss = min_running = Decimal("0")

    return {
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "realized_pnl": realized_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "min_running_pnl": min_running,
    }


def _go_no_go(
    total_pnl: Decimal,
    win_rate: Decimal,
    total_trades: int,
    max_drawdown_pct: Decimal,
) -> Tuple[str, list[str], list[str]]:
    go_conditions: list[str] = []
    no_go_conditions: list[str] = []

    if total_pnl > Decimal("0.50"):
        go_conditions.append(f"✅ Profitable: +${total_pnl}")
    elif total_pnl < Decimal("-1.00"):
        no_go_conditions.append(f"❌ Loss exceeds $1.00: ${total_pnl}")
    else:
        go_conditions.append(f"⚠️ Small P&L: ${total_pnl}")

    if win_rate >= Decimal("55"):
        go_conditions.append(f"✅ Win rate acceptable: {win_rate:.2f}%")
    else:
        no_go_conditions.append(f"❌ Win rate too low: {win_rate:.2f}%")

    if total_trades >= 5:
        go_conditions.append(f"✅ Sufficient sample size: {total_trades}")
    else:
        no_go_conditions.append(f"❌ Insufficient trades: {total_trades} (need ≥5)")

    if max_drawdown_pct < Decimal("15"):
        go_conditions.append(f"✅ Drawdown controlled: {max_drawdown_pct:.2f}%")
    else:
        no_go_conditions.append(f"❌ Drawdown too high: {max_drawdown_pct:.2f}%")

    if no_go_conditions:
        decision = "❌ NO-GO"
    elif total_pnl > 0:
        decision = "✅ GO"
    else:
        decision = "⚠️ CONDITIONAL"

    return decision, go_conditions, no_go_conditions


async def calculate_metrics(db_path: str, hours: int) -> int:
    ledger = AsyncLedger(db_path=db_path)
    await ledger.initialize()
    try:
        metrics = await _fetch_trade_metrics(ledger, hours=hours)
        current_equity = await ledger.get_equity()
        total_pnl = current_equity - STARTING_CAPITAL
        roi = (total_pnl / STARTING_CAPITAL) * Decimal("100") if STARTING_CAPITAL > 0 else Decimal("0")

        total_trades = int(metrics["total_trades"])
        winning = int(metrics["winning_trades"])
        losing = int(metrics["losing_trades"])

        closed = winning + losing
        win_rate = (Decimal(winning) / Decimal(closed) * Decimal("100")) if closed > 0 else Decimal("0")

        min_running_pnl = Decimal(str(metrics["min_running_pnl"]))
        max_drawdown_pct = (
            abs(min_running_pnl) / STARTING_CAPITAL * Decimal("100")
            if STARTING_CAPITAL > 0 and min_running_pnl < 0
            else Decimal("0")
        )

        decision, go_conditions, no_go_conditions = _go_no_go(
            total_pnl=total_pnl,
            win_rate=win_rate,
            total_trades=total_trades,
            max_drawdown_pct=max_drawdown_pct,
        )

        print("\n" + "=" * 60)
        print(f"📊 PERFORMANCE REPORT ({hours}h window)")
        print("=" * 60)
        print(f"Starting Balance: ${STARTING_CAPITAL}")
        print(f"Current Equity:   ${current_equity}")
        print(f"Total P&L:        ${total_pnl}")
        print(f"ROI:              {roi:.2f}%")
        print(f"Total Trades:     {total_trades}")
        print(f"Winning Trades:   {winning}")
        print(f"Losing Trades:    {losing}")
        print(f"Win Rate:         {win_rate:.2f}%")
        print(f"Avg Win:          ${metrics['avg_win']}")
        print(f"Avg Loss:         ${metrics['avg_loss']}")
        print(f"Realized P&L:     ${metrics['realized_pnl']}")
        print(f"Max Drawdown:     {max_drawdown_pct:.2f}%")

        print("\nGO Conditions:")
        for item in go_conditions:
            print(f"  {item}")

        if no_go_conditions:
            print("\nNO-GO Conditions:")
            for item in no_go_conditions:
                print(f"  {item}")

        print("\n" + "=" * 60)
        print(f"Decision: {decision}")
        print("=" * 60 + "\n")
        return 0
    finally:
        await ledger.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate trading metrics")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Ledger DB path")
    parser.add_argument("--hours", type=int, default=48, help="Window in hours")
    parser.add_argument("--period", type=str, default=None, help="Period, e.g. 7days")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    hours = _parse_period_to_hours(args.period) if args.period else args.hours
    raise SystemExit(asyncio.run(calculate_metrics(db_path=args.db_path, hours=hours)))
