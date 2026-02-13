from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import List

from shared.risk_aggregator import Position, UnifiedRiskAggregator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _default_positions_file(env_key: str, filename: str) -> Path:
    env_path = os.getenv(env_key)
    if env_path:
        return Path(env_path)

    if os.name == "nt":
        return Path(tempfile.gettempdir()) / filename

    return Path(f"/tmp/{filename}")


def _load_positions(path: Path) -> List[Position]:
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        logger.warning(f"Failed to read positions from {path}: {exc}")
        return []

    positions = []
    for raw in data:
        try:
            positions.append(
                Position(
                    bot=raw["bot"],
                    asset=raw["asset"],
                    direction=raw["direction"],
                    notional_value=Decimal(str(raw["notional_value"])),
                    source=raw.get("source", "unknown"),
                )
            )
        except Exception as exc:
            logger.warning(f"Invalid position payload: {exc}")
    return positions


async def monitor_risk(max_btc_exposure_usd: Decimal = Decimal("1000")) -> None:
    """
    Continuously monitor combined risk across both bots.
    Alert if limits exceeded.
    """
    aggregator = UnifiedRiskAggregator(max_btc_exposure_usd=max_btc_exposure_usd)

    crypto_path = _default_positions_file("CHARLIE_POSITIONS_FILE", "crypto_positions.json")
    poly_path = _default_positions_file("POLYMARKET_POSITIONS_FILE", "polymarket_positions.json")

    while True:
        try:
            crypto_positions = _load_positions(crypto_path)
            poly_positions = _load_positions(poly_path)

            aggregator.update_positions(crypto_positions + poly_positions)
            btc_exposure = aggregator.get_btc_exposure()

            if abs(btc_exposure) > max_btc_exposure_usd:
                logger.error(f"🚨 BTC EXPOSURE EXCEEDED: ${btc_exposure}")
            else:
                logger.info(f"✅ BTC exposure: ${btc_exposure} / ${max_btc_exposure_usd}")

        except Exception as exc:
            logger.error(f"Risk monitor error: {exc}")

        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(monitor_risk())
