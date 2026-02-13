import argparse
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from config_production import STARTING_CAPITAL
from database.ledger_async import AsyncLedger
from risk.circuit_breaker_v2 import CircuitBreakerV2

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
except ImportError as exc:
    raise SystemExit("Missing dependency: rich. Install with `pip install rich`.") from exc

console = Console()
DEFAULT_DB_PATH = "data/trading.db"
KILL_SWITCH_PATH = Path("KILL_SWITCH_ACTIVE.flag")


async def _fetch_recent_trade_rows(ledger: AsyncLedger, limit: int = 10) -> List[Dict[str, Any]]:
    rows = await ledger.execute(
        """
        SELECT
            t.timestamp,
            t.reference_id,
            t.strategy,
            COALESCE(SUM(ABS(tl.amount)), 0)
        FROM transactions t
        LEFT JOIN transaction_lines tl ON tl.transaction_id = t.id
        WHERE t.transaction_type = 'TRADE'
        GROUP BY t.id
        ORDER BY t.timestamp DESC
        LIMIT ?
        """,
        (limit,),
        fetch_all=True,
    )
    result: List[Dict[str, Any]] = []
    for row in rows or []:
        result.append(
            {
                "timestamp": str(row[0] or ""),
                "market": str(row[1] or "-")[:20],
                "strategy": str(row[2] or "-")[:16],
                "notional": Decimal(str(row[3] or "0")),
            }
        )
    return result


def _build_layout(
    equity: Decimal,
    trades: List[Dict[str, Any]],
    breaker_status: Dict[str, Any],
    started_at: float,
) -> Layout:
    pnl = equity - STARTING_CAPITAL
    pnl_pct = (pnl / STARTING_CAPITAL) * Decimal("100") if STARTING_CAPITAL > 0 else Decimal("0")

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="mid"),
        Layout(name="right"),
    )

    pnl_text = f"[green]+${pnl} ({pnl_pct:.2f}%)[/green]" if pnl >= 0 else f"[red]${pnl} ({pnl_pct:.2f}%)[/red]"
    header = (
        "[bold cyan]POLYMARKET PHASE 6 DASHBOARD[/bold cyan]\n"
        f"Equity: ${equity} | P&L: {pnl_text}"
    )
    layout["header"].update(Panel(header, border_style="cyan"))

    trades_table = Table(title="Recent Trades")
    trades_table.add_column("Time")
    trades_table.add_column("Market")
    trades_table.add_column("Strategy")
    trades_table.add_column("Notional", justify="right")
    if trades:
        for trade in trades:
            ts = trade["timestamp"]
            ts_fmt = ts[11:19] if len(ts) >= 19 else ts[:8]
            trades_table.add_row(ts_fmt, trade["market"], trade["strategy"], f"${trade['notional']}")
    else:
        trades_table.add_row("-", "-", "-", "-")
    layout["left"].update(Panel(trades_table, border_style="green"))

    cb_table = Table(title="Circuit Breaker")
    cb_table.add_column("Metric")
    cb_table.add_column("Value", justify="right")
    cb_table.add_row("State", str(breaker_status.get("state", "unknown")))
    cb_table.add_row("Loss Streak", str(breaker_status.get("consecutive_losses", 0)))
    cb_table.add_row("Trip Reason", str(breaker_status.get("trip_reason", "-")))
    cb_table.add_row("Total Trips", str(breaker_status.get("total_trips", 0)))
    cb_table.add_row("Recoveries", str(breaker_status.get("total_recoveries", 0)))
    layout["mid"].update(Panel(cb_table, border_style="yellow"))

    health_table = Table(title="System Health")
    health_table.add_column("Component")
    health_table.add_column("Status", justify="right")
    health_table.add_row("Kill Switch", "[red]ACTIVE[/red]" if KILL_SWITCH_PATH.exists() else "[green]OFF[/green]")
    uptime = int(time.time() - started_at)
    health_table.add_row("Uptime", f"{uptime}s")
    health_table.add_row("Updated", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
    layout["right"].update(Panel(health_table, border_style="blue"))

    layout["footer"].update(
        Panel("Press Ctrl+C to exit. Refresh interval configurable with --interval.", border_style="white")
    )
    return layout


async def run_dashboard(db_path: str, interval: int, once: bool) -> None:
    ledger = AsyncLedger(db_path=db_path)
    await ledger.initialize()
    equity = await ledger.get_equity()
    breaker = CircuitBreakerV2(initial_equity=equity)
    started_at = time.time()

    async def snapshot() -> Layout:
        current_equity = await ledger.get_equity()
        trades = await _fetch_recent_trade_rows(ledger, limit=10)
        breaker_status = breaker.get_status()
        return _build_layout(current_equity, trades, breaker_status, started_at)

    if once:
        console.print(await snapshot())
        await ledger.close()
        return

    with Live(await snapshot(), refresh_per_second=max(1, int(1 / max(interval, 1))), console=console) as live:
        while True:
            await asyncio.sleep(interval)
            live.update(await snapshot())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time monitoring dashboard")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Ledger DB path")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds")
    parser.add_argument("--once", action="store_true", help="Render a single snapshot and exit")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(run_dashboard(db_path=args.db_path, interval=args.interval, once=args.once))
    except KeyboardInterrupt:
        console.print("\nDashboard stopped.")
