#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import structlog
import yaml

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from database.ledger_async import AsyncLedger
from main import TradingSystem, _resolve_runtime_controls


@dataclass
class LiveProofRecommendation:
    side: str = "YES"
    size: Decimal = Decimal("10")
    kelly_fraction: Decimal = Decimal("0.1")
    p_win: float = 0.62
    p_win_raw: float = 0.64
    p_win_calibrated: float = 0.62
    implied_prob: float = 0.50
    edge: float = 0.08
    confidence: float = 0.80
    regime: str = "BULLISH"
    technical_regime: str = "TRENDING"
    reason: str = "controlled_live_db_observation_proof"
    model_votes: None = None
    ofi_conflict: bool = False


class LiveProofCharlieGate:
    def __init__(self, recommendation: LiveProofRecommendation):
        self.recommendation = recommendation

    async def evaluate_market(self, **_kwargs):
        return self.recommendation


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main() -> None:
    configure_logging()

    config_path = Path("config/production.yaml")
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = _resolve_runtime_controls(yaml.safe_load(config_file))

    db_path = str(config.get("database", {}).get("path", "data/trading.db"))
    ledger = AsyncLedger(db_path=db_path)
    await ledger.initialize()

    proof_market_id = f"controlled-live-db-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    system = TradingSystem(config)
    system.ledger = ledger
    ledger.get_equity = AsyncMock(return_value=Decimal("100"))
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.charlie_gate = LiveProofCharlieGate(LiveProofRecommendation())
    system._ensure_runtime_services()
    system._calibration_guard_status = {
        "blocked": True,
        "reason": "controlled_live_db_bad_calibration",
        "coef": -0.1,
        "monotonic": False,
    }

    try:
        await system._execute_opportunity(
            {
                "market_id": proof_market_id,
                "token_id": "token-yes",
                "side": "YES",
                "edge": Decimal("0.05"),
                "market_price": Decimal("0.41"),
                "confidence": "HIGH",
                "question": "Controlled live DB observation proof",
            },
            trigger="controlled_live_db_observation_proof",
        )

        row = await ledger.execute(
            """
            SELECT *
            FROM calibration_observations
            WHERE market_id = ? AND trigger = ?
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (proof_market_id, "controlled_live_db_observation_proof"),
            fetch_one=True,
            as_dict=True,
        )
        print(json.dumps({"db_path": db_path, "row": row}, indent=2, sort_keys=True, default=str))
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
