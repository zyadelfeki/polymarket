#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from ml.meta_promotion import finalize_staged_training


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate staged meta models, evaluate them offline, and emit a promotion report.")
    parser.add_argument("--staging-dir", default="models/meta_gate/staging", help="Directory containing Ticket 4.2 staged artifacts")
    parser.add_argument("--output-dir", default="models/meta_gate/staging/final", help="Directory for Ticket 4.3 final offline artifacts")
    parser.add_argument("--random-state", type=int, default=42, help="Deterministic random seed for calibration fitting")
    parser.add_argument("--run-id", default=None, help="Optional explicit promotion run id")
    parser.add_argument("--created-at", default=None, help="Optional explicit creation timestamp")
    parser.add_argument("--active-model-path", default=None, help="Optional explicit active runtime model path to preserve")
    parser.add_argument("--active-threshold-path", default=None, help="Optional explicit active threshold override path to preserve")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = finalize_staged_training(
        staging_dir=args.staging_dir,
        output_dir=args.output_dir,
        random_state=args.random_state,
        run_id=args.run_id,
        created_at=args.created_at,
        active_model_path=args.active_model_path,
        active_threshold_path=args.active_threshold_path,
    )
    summary = {
        "training_report_path": report["training_report_path"],
        "promotable_model_bundle_path": report["promotable_model_bundle_path"],
        "promotion_passed": report["promotion_gate"]["passed"],
        "gate_reason_codes": [reason["code"] for reason in report["promotion_gate"]["reasons"]],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()