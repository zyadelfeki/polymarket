#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
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
    parser = argparse.ArgumentParser(description="Materialize offline meta datasets from calibration observations.")
    parser.add_argument("--config", default="config/production.yaml", help="Path to YAML config file")
    parser.add_argument("--db-path", default=None, help="Explicit SQLite DB path override")
    parser.add_argument("--candidate-exhaust-path", default=None, help="Output CSV for candidate_exhaust")
    parser.add_argument("--executed-profitability-path", default=None, help="Output CSV for executed_profitability")
    parser.add_argument("--split-manifest-path", default=None, help="Output JSON for deterministic training split manifest")
    parser.add_argument("--min-positive-return-bps", default=None, help="Minimum realized return in bps for profitability label=1")
    parser.add_argument("--min-fill-ratio", default=None, help="Minimum fill ratio required for executed_profitability inclusion")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    config_path = Path(args.config)
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = _resolve_runtime_controls(yaml.safe_load(config_file))

    db_path = str(args.db_path or config.get("database", {}).get("path", "data/trading.db"))
    calibration_cfg = config.get("runtime_controls", {}).get("calibration", {})
    candidate_exhaust_path = str(
        args.candidate_exhaust_path
        or calibration_cfg.get("candidate_exhaust_path", "data/candidate_exhaust.csv")
    )
    executed_profitability_path = str(
        args.executed_profitability_path
        or calibration_cfg.get("executed_profitability_path", "data/executed_profitability.csv")
    )
    split_manifest_path = str(
        args.split_manifest_path
        or calibration_cfg.get("split_manifest_path", "models/meta_gate/split_manifest.json")
    )
    min_positive_return_bps = args.min_positive_return_bps or calibration_cfg.get("min_positive_return_bps", "0")
    min_fill_ratio = args.min_fill_ratio or calibration_cfg.get("min_fill_ratio", "1.0")

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
            feature_schema_version=str(
                calibration_cfg.get("meta_candidate_feature_schema_version", "meta_candidate_v1")
            ),
            cluster_policy_version=str(
                calibration_cfg.get("meta_candidate_cluster_policy_version", "cluster_v1")
            ),
            cluster_time_bucket_seconds=int(
                calibration_cfg.get("meta_candidate_cluster_time_bucket_seconds", 10)
            ),
            cluster_price_bucket_abs=str(
                calibration_cfg.get("meta_candidate_cluster_price_bucket_abs", "0.01")
            ),
        )
        report = await service.materialize_meta_datasets(
            candidate_exhaust_path=candidate_exhaust_path,
            executed_profitability_path=executed_profitability_path,
            split_manifest_path=split_manifest_path,
            min_positive_return_bps=min_positive_return_bps,
            min_fill_ratio=min_fill_ratio,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(main())
