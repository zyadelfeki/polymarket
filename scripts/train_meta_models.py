#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ml.meta_training import write_training_artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train offline baseline and primary meta models from staged split inputs.")
    parser.add_argument("--executed-profitability-path", required=True, help="Path to executed_profitability.csv")
    parser.add_argument("--split-manifest-path", required=True, help="Path to split_manifest.json")
    parser.add_argument("--output-dir", default="models/meta_gate/staging", help="Directory for staged training artifacts")
    parser.add_argument("--random-state", type=int, default=42, help="Deterministic random seed")
    parser.add_argument("--run-id", default=None, help="Optional explicit run id for deterministic outputs")
    parser.add_argument("--created-at", default=None, help="Optional explicit creation timestamp for deterministic outputs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = write_training_artifacts(
        executed_profitability_path=args.executed_profitability_path,
        split_manifest_path=args.split_manifest_path,
        output_dir=args.output_dir,
        run_id=args.run_id,
        created_at=args.created_at,
        random_state=args.random_state,
    )
    summary = {
        "model_path": report["model_path"],
        "feature_schema_path": report["feature_schema_path"],
        "run_metadata_path": report["run_metadata_path"],
        "model_version": report["model_payload"]["model_version"],
        "baseline_model_type": report["model_payload"]["baseline_model_type"],
        "primary_model_type": report["model_payload"]["primary_model_type"],
        "train_row_count": report["run_metadata"]["train_row_count"],
        "validation_row_count": report["run_metadata"]["validation_row_count"],
        "test_row_count": report["run_metadata"]["test_row_count"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()