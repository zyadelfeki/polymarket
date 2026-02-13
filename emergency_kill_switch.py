import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from database.ledger_async import AsyncLedger
from data_feeds.polymarket_client_v2 import PolymarketClientV2

KILL_SWITCH_PATH = Path("KILL_SWITCH_ACTIVE.flag")
DEFAULT_DB_PATH = "data/trading.db"
STARTING_BALANCE = Decimal("13.98")


async def emergency_shutdown(reason: str, db_path: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    print("🚨 EMERGENCY SHUTDOWN INITIATED")
    print(f"Reason: {reason}")
    print(f"Time: {now}")

    KILL_SWITCH_PATH.write_text(f"EMERGENCY STOP: {reason}\nTime: {now}\n", encoding="utf-8")
    print("✅ Kill switch flag created")

    final_equity = Decimal("0")
    ledger = AsyncLedger(db_path=db_path)
    try:
        await ledger.initialize()
        final_equity = await ledger.get_equity()
        await ledger.record_audit_event(
            entity_type="system",
            entity_id="kill_switch",
            old_state="RUNNING",
            new_state="STOPPED",
            reason="emergency_stop",
            context={"reason": reason, "equity": str(final_equity)},
            correlation_id="kill_switch_manual",
        )
        print(f"✅ Emergency stop logged (equity=${final_equity})")
    except Exception as exc:
        print(f"⚠️ Ledger access failed: {exc}")
    finally:
        await ledger.close()

    client = PolymarketClientV2(paper_trading=True)
    try:
        if hasattr(client, "get_open_orders"):
            open_orders = await client.get_open_orders()  # type: ignore[attr-defined]
            if open_orders:
                print(f"⚠️ Found {len(open_orders)} open orders; attempting cancel")
                for order in open_orders:
                    order_id = order.get("id") or order.get("order_id")
                    if order_id:
                        await client.cancel_order(order_id)
                print("✅ Open order cancellation attempted")
            else:
                print("✅ No open orders detected")
        else:
            print("ℹ️ Client has no get_open_orders API; verify open orders manually in UI")
    except Exception as exc:
        print(f"⚠️ Order cancellation check failed: {exc}")
    finally:
        await client.close()

    pnl = final_equity - STARTING_BALANCE
    print("=" * 60)
    print("🛑 BOT STOPPED - KILL SWITCH ACTIVE")
    print(f"Final equity: ${final_equity}")
    print(f"Starting balance: ${STARTING_BALANCE}")
    print(f"P&L: ${pnl}")
    print("To restart: investigate issue, delete KILL_SWITCH_ACTIVE.flag, then relaunch bot")
    print("=" * 60)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emergency kill switch")
    parser.add_argument("--reason", required=True, help="Reason for emergency stop")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Ledger DB path")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(emergency_shutdown(reason=args.reason, db_path=args.db_path)))
