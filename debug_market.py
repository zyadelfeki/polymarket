"""
Diagnostic: inspect the one BTC price-level market surviving the scanner's
_looks_like_price_level_market() filter and report every field the expiry
block reads, so we can see exactly why after_expiry_filter stays 0.

Run:
    python debug_market.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.environ.get("CHARLIE_PATH", ""))
os.environ["PAPER_TRADING"] = "true"

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from strategies.btc_price_level_scanner import BTCPriceLevelScanner


async def main():
    client = PolymarketClientV2(paper_trading=True)
    scanner = BTCPriceLevelScanner()

    markets = await client.get_active_markets(limit=200)
    btc_matches = [m for m in markets if scanner._looks_like_price_level_market(m)]
    print(f"BTC price-level matches: {len(btc_matches)}")

    now = datetime.now(timezone.utc)

    for m in btc_matches:
        mid    = str(m.get("id") or m.get("condition_id") or "")
        q      = (m.get("question") or "")[:70]
        edt    = scanner._extract_market_datetime(m)
        closed = scanner._is_market_closed(m)
        active = m.get("active")
        status = m.get("status")
        yes_p  = m.get("yes_price")
        no_p   = m.get("no_price")

        if edt is None:
            expired_str = "NO_DATE"
        elif edt < now:
            expired_str = f"EXPIRED (was {edt.isoformat()})"
        else:
            mins_left = int((edt - now).total_seconds() / 60)
            expired_str = f"FUTURE in {mins_left}m (until {edt.isoformat()})"

        print(f"  id      = {mid[:16]}")
        print(f"  closed  = {closed}  active={active}  status={status}")
        print(f"  expiry  = {expired_str}")
        print(f"  yes_p   = {yes_p}  no_p={no_p}")
        print(f"  q       = {q}")

        # Show every date-ish key present in the raw market dict
        date_keys = [
            "end_date", "endDate", "resolution_date", "resolve_date",
            "closedTime", "end_date_iso", "endDateIso", "endDateISO",
            "endTime", "end_time", "closeTime", "close_time", "closes_at",
            "resolve_time", "resolution_time", "resolutionTime",
            "expires_at", "expiresAt",
        ]
        found_keys = {k: m[k] for k in date_keys if m.get(k)}
        print(f"  date_keys_present = {found_keys}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
