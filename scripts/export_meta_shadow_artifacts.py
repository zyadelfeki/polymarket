#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import ml.meta_gate as meta_gate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export shadow session summary and replay agreement artifacts without touching live execution paths."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    session_parser = subparsers.add_parser(
        "session-summary",
        help="Export a shadow session summary JSON artifact.",
    )
    session_parser.add_argument("--output-path", required=True, help="Path to write the session summary JSON")
    session_parser.add_argument("--session-id", default=None, help="Optional explicit session id")
    session_parser.add_argument("--exported-at", default=None, help="Optional explicit export timestamp")
    session_parser.add_argument(
        "--replay-input-path",
        default=None,
        help="Optional JSON or JSONL replay batch file used to populate the in-process summary before export",
    )
    session_parser.add_argument(
        "--replay-input-format",
        choices=["auto", "json", "jsonl"],
        default="auto",
        help="Replay input format when --replay-input-path is provided",
    )
    session_parser.add_argument(
        "--expected-feature-schema-version",
        default=None,
        help="Expected feature schema version for replay-backed session summaries",
    )
    session_parser.add_argument(
        "--calibration-version",
        default="platt_scaler_v1",
        help="Calibration version label used during replay-backed session summaries",
    )
    session_parser.add_argument(
        "--promotable-model-bundle-path",
        default=None,
        help="Optional explicit promoted bundle path for shadow replay scoring",
    )
    session_parser.add_argument(
        "--promotion-report-path",
        default=None,
        help="Optional explicit training report path for shadow replay scoring",
    )

    replay_parser = subparsers.add_parser(
        "replay-agreement",
        help="Export a shadow replay agreement JSON artifact from a batch input file.",
    )
    replay_parser.add_argument("--input-path", required=True, help="Replay batch input path in JSON or JSONL format")
    replay_parser.add_argument(
        "--input-format",
        choices=["auto", "json", "jsonl"],
        default="auto",
        help="Replay input format",
    )
    replay_parser.add_argument("--output-path", required=True, help="Path to write the replay agreement JSON")
    replay_parser.add_argument(
        "--expected-feature-schema-version",
        required=True,
        help="Expected feature schema version used for replay scoring",
    )
    replay_parser.add_argument(
        "--calibration-version",
        default="platt_scaler_v1",
        help="Calibration version label used during replay scoring",
    )
    replay_parser.add_argument("--report-id", default=None, help="Optional explicit replay report id")
    replay_parser.add_argument("--exported-at", default=None, help="Optional explicit export timestamp")
    replay_parser.add_argument(
        "--tolerance",
        type=float,
        default=meta_gate.SHADOW_REPLAY_P_PROFIT_TOLERANCE,
        help="Absolute tolerance for p_profit agreement checks",
    )
    replay_parser.add_argument(
        "--mismatch-sample-limit",
        type=int,
        default=5,
        help="Maximum mismatch examples to include in the exported report",
    )
    replay_parser.add_argument(
        "--promotable-model-bundle-path",
        default=None,
        help="Optional explicit promoted bundle path for shadow replay scoring",
    )
    replay_parser.add_argument(
        "--promotion-report-path",
        default=None,
        help="Optional explicit training report path for shadow replay scoring",
    )
    return parser.parse_args()


def _resolve_input_format(input_path: Path, requested_format: str) -> str:
    if requested_format != "auto":
        return requested_format
    suffix = input_path.suffix.lower()
    if suffix == ".jsonl":
        return "jsonl"
    if suffix == ".json":
        return "json"
    raise ValueError(
        f"unable to infer replay input format from '{input_path}'. Use --input-format json or jsonl explicitly"
    )


def _validate_feature_batches(feature_batches: object, *, source: str) -> List[dict]:
    if not isinstance(feature_batches, list):
        raise ValueError(f"{source} must contain a list of feature payload objects")
    validated_batches: List[dict] = []
    for index, feature_payload in enumerate(feature_batches, start=1):
        if not isinstance(feature_payload, dict):
            raise ValueError(f"{source} item {index} must be a JSON object")
        validated_batches.append(dict(feature_payload))
    if not validated_batches:
        raise ValueError(f"{source} is empty; provide at least one feature payload")
    return validated_batches


def load_feature_batches(input_path: str | Path, *, input_format: str = "auto") -> List[dict]:
    resolved_path = Path(input_path)
    if not resolved_path.exists():
        raise ValueError(f"replay input path does not exist: {resolved_path}")

    resolved_format = _resolve_input_format(resolved_path, input_format)
    if resolved_format == "json":
        try:
            payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON replay input in {resolved_path}: {exc.msg}") from exc
        if isinstance(payload, dict) and "feature_batches" in payload:
            return _validate_feature_batches(payload.get("feature_batches"), source=f"{resolved_path} feature_batches")
        return _validate_feature_batches(payload, source=str(resolved_path))

    feature_batches: List[dict] = []
    for line_number, raw_line in enumerate(resolved_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped_line = raw_line.strip()
        if not stripped_line:
            continue
        try:
            payload = json.loads(stripped_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid JSONL replay input in {resolved_path} at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"JSONL replay input line {line_number} in {resolved_path} must be a JSON object")
        feature_batches.append(dict(payload))
    if not feature_batches:
        raise ValueError(f"{resolved_path} is empty; provide at least one JSONL feature payload")
    return feature_batches


def _configure_shadow_artifact_paths(args: argparse.Namespace) -> None:
    changed = False
    if getattr(args, "promotable_model_bundle_path", None):
        meta_gate._PROMOTABLE_MODEL_BUNDLE_PATH = Path(str(args.promotable_model_bundle_path))
        changed = True
    if getattr(args, "promotion_report_path", None):
        meta_gate._PROMOTION_REPORT_PATH = Path(str(args.promotion_report_path))
        changed = True
    if changed:
        meta_gate.reload_shadow_runtime_bundle()


def _prime_session_summary_from_replay(args: argparse.Namespace) -> None:
    if not args.replay_input_path:
        return
    if not args.expected_feature_schema_version:
        raise ValueError(
            "--expected-feature-schema-version is required when --replay-input-path is provided"
        )

    feature_batches = load_feature_batches(
        args.replay_input_path,
        input_format=args.replay_input_format,
    )
    for feature_payload in feature_batches:
        meta_gate.evaluate_runtime_decision(
            feature_payload,
            expected_feature_schema_version=str(args.expected_feature_schema_version),
            calibration_version=str(args.calibration_version),
        )


def run_session_summary(args: argparse.Namespace) -> dict:
    _configure_shadow_artifact_paths(args)
    _prime_session_summary_from_replay(args)
    summary = meta_gate.export_shadow_session_summary(
        args.output_path,
        session_id=args.session_id,
        exported_at=args.exported_at,
    )
    return {
        "command": "session-summary",
        "output_path": str(Path(args.output_path)),
        "session_id": summary["session_id"],
        "shadow_decisions": summary["decision_counts"]["shadow_decisions"],
    }


def run_replay_agreement(args: argparse.Namespace) -> dict:
    _configure_shadow_artifact_paths(args)
    feature_batches = load_feature_batches(args.input_path, input_format=args.input_format)
    report = meta_gate.export_shadow_replay_agreement_report(
        feature_batches,
        args.output_path,
        expected_feature_schema_version=str(args.expected_feature_schema_version),
        calibration_version=str(args.calibration_version),
        tolerance=float(args.tolerance),
        mismatch_sample_limit=int(args.mismatch_sample_limit),
        report_id=args.report_id,
        exported_at=args.exported_at,
    )
    return {
        "command": "replay-agreement",
        "output_path": str(Path(args.output_path)),
        "report_id": report["report_id"],
        "input_count": report["input_count"],
        "disagreement_examples_count": report["disagreement_examples_count"],
    }


def main() -> None:
    args = parse_args()
    try:
        if args.command == "session-summary":
            result = run_session_summary(args)
        else:
            result = run_replay_agreement(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()