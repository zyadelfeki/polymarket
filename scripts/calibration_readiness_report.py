#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from database.ledger_async import AsyncLedger
from main import _resolve_runtime_controls
from services.calibration_observation_service import CalibrationObservationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print calibration readiness from a specific SQLite DB.")
    parser.add_argument("--config", default="config/production.yaml", help="Path to YAML config file")
    parser.add_argument("--db-path", default=None, help="Explicit SQLite DB path override")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = _resolve_runtime_controls(yaml.safe_load(config_file))

    db_path = str(args.db_path or config.get("database", {}).get("path", "data/trading.db"))
    calibration_cfg = config.get("runtime_controls", {}).get("calibration", {})

    ledger = AsyncLedger(db_path=db_path)
    await ledger.initialize()
    try:
        service = CalibrationObservationService(
            ledger=ledger,
            observation_export_path=str(
                calibration_cfg.get(
                    "observation_export_path",
                    calibration_cfg.get("observation_store_path", "data/calibration_observations.csv"),
                )
            ),
            dataset_export_path=str(
                calibration_cfg.get(
                    "dataset_export_path",
                    calibration_cfg.get("dataset_path", "data/calibration_dataset_v2.csv"),
                )
            ),
        )
        report = await service.build_readiness_report()
        print(json.dumps({"db_path": db_path, **report}, indent=2, sort_keys=True))
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
