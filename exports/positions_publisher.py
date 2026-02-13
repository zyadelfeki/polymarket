from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Union

from database.ledger import Ledger
from shared.risk_aggregator import Position

logger = logging.getLogger(__name__)


class PolymarketPositionsPublisher:
    """
    Export current Polymarket positions to a shared file for unified risk checks.
    """

    def __init__(self, ledger: Ledger, shared_file: Optional[Union[str, Path]] = None):
        self.ledger = ledger
        if shared_file is None:
            shared_file = self._default_shared_file()
        self.shared_file = Path(shared_file)

    def publish_positions(self) -> None:
        positions = build_positions_from_ledger(self.ledger)
        serialized = [self._serialize_position(p) for p in positions]

        self.shared_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.shared_file.with_suffix(self.shared_file.suffix + ".tmp")
        temp_file.write_text(json.dumps(serialized, indent=2))
        temp_file.replace(self.shared_file)

    def _serialize_position(self, position: Position) -> Dict[str, str]:
        data = asdict(position)
        data["notional_value"] = str(position.notional_value)
        return data

    def _default_shared_file(self) -> str:
        env_path = os.getenv("POLYMARKET_POSITIONS_FILE")
        if env_path:
            return env_path

        if os.name == "nt":
            return str(Path(tempfile.gettempdir()) / "polymarket_positions.json")

        return "/tmp/polymarket_positions.json"


def build_positions_from_ledger(ledger: Ledger) -> List[Position]:
    """
    Convert open ledger positions to Position objects for BTC exposure.
    """
    positions = []
    open_positions = ledger.get_open_positions()

    for pos in open_positions:
        metadata = pos.get("metadata")
        if metadata and isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        elif metadata is None:
            metadata = {}

        question = (metadata.get("question") or metadata.get("market_question") or "").lower()
        market_id = str(pos.get("market_id", ""))

        is_btc_market = "btc" in question or "bitcoin" in question or "btc" in market_id.lower()
        if not is_btc_market:
            continue

        is_below = any(word in question for word in ["below", "under", "less than", "<"])

        side = str(pos.get("side", "")).upper()
        if side not in {"YES", "NO"}:
            continue

        if is_below:
            yes_direction = "SHORT"
            no_direction = "LONG"
        else:
            yes_direction = "LONG"
            no_direction = "SHORT"

        direction = yes_direction if side == "YES" else no_direction

        quantity = Decimal(str(pos.get("quantity", 0)))
        current_price = pos.get("current_price")
        entry_price = pos.get("entry_price")

        price = Decimal(str(current_price if current_price is not None else entry_price or 0))
        notional_value = quantity * price

        positions.append(
            Position(
                bot="polymarket",
                asset="BTC",
                direction=direction,
                notional_value=notional_value,
                source="polymarket",
            )
        )

    return positions
