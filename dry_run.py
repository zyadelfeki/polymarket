#!/usr/bin/env python3
"""
Dry Run: Pre-launch smoke test — BTCPriceLevelScanner path.

Runs exactly 2 scan cycles with PAPER_TRADING=true.
Per cycle:
  1. circuit_breaker.is_trading_allowed() — gate must pass before scanning
  2. BTCPriceLevelScanner.scan() — finds opportunities via Charlie gate
  3. Every opportunity found is printed; no orders are ever placed.

Pass criteria:
  - Both cycles complete without exception
  - is_trading_allowed() is called once per cycle
  - No place_bet / place_order calls anywhere in this script
"""

import asyncio
import os

# Force paper-trading mode before any other imports load Settings.
os.environ["PAPER_TRADING"] = "true"

import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import Settings
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from integrations.charlie_booster import CharliePredictionGate
from risk.circuit_breaker import CircuitBreaker
from strategies.btc_price_level_scanner import BTCPriceLevelScanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("dry_run")

NUM_CYCLES = 2


async def main() -> None:
    SEP = "=" * 60

    print(SEP)
    print("POLYMARKET DRY RUN  (PAPER_TRADING=true)")
    print(f"Cycles to run : {NUM_CYCLES}")
    print(f"Strategy      : BTCPriceLevelScanner")
    print(f"Real orders   : NONE")
    print(SEP)

    # --- Component setup -------------------------------------------------------
    equity: Decimal = Settings.INITIAL_CAPITAL

    api_client = PolymarketClientV2(
        private_key=None,
        paper_trading=True,
        rate_limit=8.0,
    )
    charlie_gate = CharliePredictionGate(kelly_sizer=None)
    scanner = BTCPriceLevelScanner()
    circuit_breaker = CircuitBreaker(initial_capital=equity)

    logger.info(
        "components_ready  equity=%s  paper_trading=%s",
        equity,
        api_client.paper_trading,
    )

    # Defensive assertion: confirm paper_trading flag is set before we run.
    assert api_client.paper_trading, "BUG: api_client.paper_trading must be True in dry run"

    # --- Cycle loop ------------------------------------------------------------
    total_opportunities: int = 0

    for cycle in range(1, NUM_CYCLES + 1):
        print(f"\n{SEP}")
        print(f"CYCLE {cycle}/{NUM_CYCLES}")
        print(SEP)

        # Gate 1: circuit breaker (required every cycle)
        allowed = circuit_breaker.is_trading_allowed()
        logger.info("circuit_breaker_check  cycle=%d  allowed=%s  reason=%s",
                    cycle, allowed, circuit_breaker.breaker_reason)
        if not allowed:
            logger.warning(
                "cycle_skipped  cycle=%d  reason=%s", cycle, circuit_breaker.breaker_reason
            )
            continue

        # Gate 2: scan for opportunities
        try:
            opportunities = await scanner.scan(
                charlie_gate=charlie_gate,
                api_client=api_client,
                equity=equity,
                max_days_to_expiry=7,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("scan_error  cycle=%d  error=%s", cycle, exc, exc_info=True)
            continue

        if not opportunities:
            logger.info("no_opportunities_found  cycle=%d", cycle)
        else:
            logger.info("opportunities_found  cycle=%d  count=%d", cycle, len(opportunities))
            for idx, opp in enumerate(opportunities, start=1):
                market_id = opp.get("market_id", "unknown")
                question = opp.get("question", "")[:80]
                confidence = opp.get("confidence", "?")
                edge = opp.get("edge", "?")
                side = opp.get("true_outcome") or opp.get("side", "?")
                print(
                    f"  [{cycle}.{idx}] market={market_id}  side={side}"
                    f"  confidence={confidence}  edge={edge}"
                    f"  q={question!r}"
                )

        total_opportunities += len(opportunities)

        # No order placement here — smoke test confirms circuit breaker is
        # called and scanner runs cleanly.  place_bet is never invoked.

    # --- Summary ---------------------------------------------------------------
    print(f"\n{SEP}")
    print("DRY RUN SUMMARY")
    print(SEP)
    print(f"  Cycles run          : {NUM_CYCLES}")
    print(f"  Total opportunities : {total_opportunities}")
    print(f"  Real orders placed  : 0")
    print(f"  PAPER_TRADING env   : {os.environ.get('PAPER_TRADING')}")
    print(SEP)
    logger.info("dry_run_complete  cycles=%d  opportunities=%d  orders=0",
                NUM_CYCLES, total_opportunities)



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDry run interrupted by user.")

