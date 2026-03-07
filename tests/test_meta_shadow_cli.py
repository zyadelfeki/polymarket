from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.test_meta_gate import _build_promotable_shadow_artifacts
from tests.test_meta_training import _make_training_rows, _write_validated_inputs


def _script_path() -> Path:
    return Path(__file__).resolve().parent.parent / "scripts" / "export_meta_shadow_artifacts.py"


def _write_json_input(path: Path, feature_batches: list[dict]) -> Path:
    path.write_text(json.dumps(feature_batches, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _write_jsonl_input(path: Path, feature_batches: list[dict]) -> Path:
    lines = [json.dumps(feature_payload, sort_keys=True) for feature_payload in feature_batches]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _valid_feature_payload() -> dict:
    return {
        "selected_side_is_yes": 1.0,
        "raw_yes_prob": 0.71,
        "yes_side_raw_probability": 0.71,
        "calibrated_yes_prob": 0.67,
        "selected_side_prob": 0.67,
        "charlie_confidence": 0.82,
        "charlie_implied_prob": 0.49,
        "charlie_edge": 0.08,
        "spread_bps": 90.0,
        "time_to_expiry_seconds": 3200.0,
        "token_price": 0.43,
        "normalized_yes_price": 0.43,
    }


def _make_promotable_training_rows(total_rows: int = 120) -> list[dict]:
    rows = _make_training_rows(total_rows)
    for index, row in enumerate(rows):
        if index % 7 == 0:
            flipped_label = 1 - int(row["profitability_label"])
            row["profitability_label"] = flipped_label
            row["actual_yes_outcome"] = str(flipped_label)
            row["eventual_yes_market_outcome"] = str(flipped_label)
            row["settled_pnl"] = "2.00000000" if flipped_label else "-1.00000000"
            row["realized_return_bps"] = "200.000000" if flipped_label else "-100.000000"
    return rows


def test_cli_writes_session_summary_artifact(tmp_path):
    bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
    input_path = _write_json_input(
        tmp_path / "session_input.json",
        [_valid_feature_payload(), {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71}],
    )
    output_path = tmp_path / "shadow_session_summary.json"
    repo_root = Path(__file__).resolve().parent.parent

    completed = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "session-summary",
            "--output-path",
            str(output_path),
            "--session-id",
            "cli-shadow-session",
            "--exported-at",
            "2026-03-07T13:00:00Z",
            "--replay-input-path",
            str(input_path),
            "--replay-input-format",
            "json",
            "--expected-feature-schema-version",
            "meta_candidate_v1",
            "--promotable-model-bundle-path",
            str(bundle_path),
            "--promotion-report-path",
            str(report_path),
        ],
        check=True,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert '"command": "session-summary"' in completed.stdout
    assert str(output_path) in completed.stdout
    assert payload["schema_version"] == "shadow_session_summary_v1"
    assert payload["session_id"] == "cli-shadow-session"
    assert payload["exported_at"] == "2026-03-07T13:00:00Z"
    assert payload["decision_counts"]["shadow_decisions"] == 2
    assert payload["decision_counts"]["valid_promoted_bundle_decisions"] == 1
    assert payload["decision_counts"]["schema_mismatch_decisions"] == 1
    assert payload["decision_counts"]["fallback_decisions"] == 1
    assert payload["artifact_load_status_summary"] == {"loaded": 2}
    assert payload["observational_contract"]["effective_allow_trade_true_count"] == 2
    assert payload["observational_contract"]["shadow_only_true_count"] == 2


def test_cli_writes_replay_agreement_artifact(tmp_path):
    bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
    input_path = _write_jsonl_input(
        tmp_path / "replay_input.jsonl",
        [_valid_feature_payload(), {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71}],
    )
    output_path = tmp_path / "shadow_replay_agreement.json"
    repo_root = Path(__file__).resolve().parent.parent

    completed = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "replay-agreement",
            "--input-path",
            str(input_path),
            "--input-format",
            "jsonl",
            "--output-path",
            str(output_path),
            "--expected-feature-schema-version",
            "meta_candidate_v1",
            "--report-id",
            "cli-shadow-replay",
            "--exported-at",
            "2026-03-07T13:05:00Z",
            "--promotable-model-bundle-path",
            str(bundle_path),
            "--promotion-report-path",
            str(report_path),
        ],
        check=True,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert '"command": "replay-agreement"' in completed.stdout
    assert str(output_path) in completed.stdout
    assert payload["schema_version"] == "shadow_replay_agreement_v1"
    assert payload["report_id"] == "cli-shadow-replay"
    assert payload["exported_at"] == "2026-03-07T13:05:00Z"
    assert payload["input_count"] == 2
    assert payload["valid_scored_count"] == 1
    assert payload["fallback_count"] == 0
    assert payload["schema_mismatch_count"] == 1
    assert payload["artifact_load_status_summary"] == {"loaded": 2}
    assert payload["p_profit_match"]["exact_match_rate"] == pytest.approx(1.0)
    assert payload["threshold_interpretation"]["agreement_rate"] == pytest.approx(1.0)
    assert payload["disagreement_examples_count"] == 0
    assert payload["observational_contract"]["effective_allow_trade_true_count"] == 2


def test_cli_malformed_replay_input_fails_clearly(tmp_path):
    malformed_path = tmp_path / "bad_input.jsonl"
    malformed_path.write_text('{"selected_side_is_yes": 1.0}\nnot-json\n', encoding="utf-8")
    output_path = tmp_path / "unused.json"
    repo_root = Path(__file__).resolve().parent.parent

    completed = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "replay-agreement",
            "--input-path",
            str(malformed_path),
            "--input-format",
            "jsonl",
            "--output-path",
            str(output_path),
            "--expected-feature-schema-version",
            "meta_candidate_v1",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "ERROR: invalid JSONL replay input" in completed.stderr
    assert "line 2" in completed.stderr
    assert output_path.exists() is False


def test_cli_module_has_no_main_dependency(monkeypatch):
    main_stub = types.ModuleType("main")

    def _raise_on_access(name: str):
        raise AssertionError(f"unexpected main module dependency: {name}")

    main_stub.__getattr__ = _raise_on_access  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "main", main_stub)
    monkeypatch.delitem(sys.modules, "scripts.export_meta_shadow_artifacts", raising=False)

    module = importlib.import_module("scripts.export_meta_shadow_artifacts")

    assert hasattr(module, "load_feature_batches")
    assert hasattr(module, "run_session_summary")
    assert hasattr(module, "run_replay_agreement")


def test_cli_round_trip_from_validated_inputs_to_promoted_runtime_exports(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    inputs_dir = tmp_path / "inputs"
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "final"
    export_dir = tmp_path / "exports"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    executed_path, split_manifest_path, _ = _write_validated_inputs(
        inputs_dir,
        rows=_make_promotable_training_rows(120),
    )

    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "train_meta_models.py"),
            "--executed-profitability-path",
            str(executed_path),
            "--split-manifest-path",
            str(split_manifest_path),
            "--output-dir",
            str(staging_dir),
            "--run-id",
            "cli-roundtrip-stage",
            "--created-at",
            "2026-03-07T15:00:00Z",
        ],
        check=True,
        cwd=repo_root,
    )
    subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "promote_meta_models.py"),
            "--staging-dir",
            str(staging_dir),
            "--output-dir",
            str(final_dir),
            "--run-id",
            "cli-roundtrip-final",
            "--created-at",
            "2026-03-07T15:10:00Z",
        ],
        check=True,
        cwd=repo_root,
    )

    session_input_path = _write_json_input(
        export_dir / "session_input.json",
        [_valid_feature_payload(), {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71}],
    )
    replay_input_path = _write_jsonl_input(
        export_dir / "replay_input.jsonl",
        [_valid_feature_payload(), {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71}],
    )
    session_output_path = export_dir / "session_summary.json"
    replay_output_path = export_dir / "replay_agreement.json"
    bundle_path = final_dir / "promotable_model_bundle.joblib"
    report_path = final_dir / "training_report.json"

    subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "session-summary",
            "--output-path",
            str(session_output_path),
            "--session-id",
            "cli-roundtrip-session",
            "--exported-at",
            "2026-03-07T15:20:00Z",
            "--replay-input-path",
            str(session_input_path),
            "--replay-input-format",
            "json",
            "--expected-feature-schema-version",
            "meta_candidate_v1",
            "--promotable-model-bundle-path",
            str(bundle_path),
            "--promotion-report-path",
            str(report_path),
        ],
        check=True,
        cwd=repo_root,
    )
    subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "replay-agreement",
            "--input-path",
            str(replay_input_path),
            "--input-format",
            "jsonl",
            "--output-path",
            str(replay_output_path),
            "--expected-feature-schema-version",
            "meta_candidate_v1",
            "--report-id",
            "cli-roundtrip-replay",
            "--exported-at",
            "2026-03-07T15:25:00Z",
            "--promotable-model-bundle-path",
            str(bundle_path),
            "--promotion-report-path",
            str(report_path),
        ],
        check=True,
        cwd=repo_root,
    )

    training_report = json.loads(report_path.read_text(encoding="utf-8"))
    session_payload = json.loads(session_output_path.read_text(encoding="utf-8"))
    replay_payload = json.loads(replay_output_path.read_text(encoding="utf-8"))

    assert training_report["promotion_gate"]["passed"] is True
    assert bundle_path.exists() is True
    assert session_payload["decision_counts"]["shadow_decisions"] == 2
    assert session_payload["decision_counts"]["valid_promoted_bundle_decisions"] == 1
    assert session_payload["decision_counts"]["schema_mismatch_decisions"] == 1
    assert replay_payload["valid_scored_count"] == 1
    assert replay_payload["schema_mismatch_count"] == 1
    assert replay_payload["disagreement_examples_count"] == 0


def test_main_module_has_no_meta_shadow_runtime_wiring():
    main_path = Path(__file__).resolve().parent.parent / "main.py"
    main_source = main_path.read_text(encoding="utf-8")

    assert "evaluate_runtime_decision as _meta_gate_evaluate_runtime_decision" not in main_source
    assert "extract_features_from_opportunity as _meta_gate_extract_features" not in main_source
    assert "meta_gate_shadow_runtime_decision" not in main_source
    assert "meta_shadow_decisions" not in main_source