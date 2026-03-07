from __future__ import annotations

import json
from pathlib import Path

import joblib

from ml import meta_gate, meta_promoted_contract, meta_promotion, meta_training
from tests.test_meta_training import _make_training_rows, _write_validated_inputs


def _reset_shadow_loader_state() -> None:
    meta_gate._SHADOW_MODEL_CACHE = meta_gate._NOT_LOADED
    meta_gate._SHADOW_MODEL_LOAD_ATTEMPTED = False
    meta_gate._meta_gate_shadow_load_successes = 0
    meta_gate._meta_gate_shadow_load_failures = 0
    meta_gate._meta_gate_shadow_load_failure_reasons = {}
    meta_gate._meta_gate_shadow_last_load_failure_reason = None


def _build_promoted_artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    rows = _make_training_rows(120)
    for index, row in enumerate(rows):
        if index % 7 == 0:
            flipped_label = 1 - int(row["profitability_label"])
            row["profitability_label"] = flipped_label
            row["actual_yes_outcome"] = str(flipped_label)
            row["eventual_yes_market_outcome"] = str(flipped_label)
            row["settled_pnl"] = "2.00000000" if flipped_label else "-1.00000000"
            row["realized_return_bps"] = "200.000000" if flipped_label else "-100.000000"

    inputs_dir = tmp_path / "inputs"
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "final"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    executed_path, split_manifest_path, _ = _write_validated_inputs(inputs_dir, rows=rows)
    meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(staging_dir),
        run_id="ticket-4-5-stage",
        created_at="2026-03-07T09:00:00Z",
        random_state=42,
    )
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(final_dir),
        run_id="ticket-4-5-final",
        created_at="2026-03-07T09:10:00Z",
    )
    bundle_path = Path(report["promotable_model_bundle_path"])
    report_path = Path(report["training_report_path"])
    feature_schema_path = Path(joblib.load(bundle_path)["staged_feature_schema_path"])
    return bundle_path, report_path, feature_schema_path


def _shadow_feature_payload() -> dict:
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


def test_producer_writes_artifacts_conforming_to_shared_contract(tmp_path):
    bundle_path, report_path, feature_schema_path = _build_promoted_artifacts(tmp_path)

    bundle = joblib.load(bundle_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    feature_schema = json.loads(feature_schema_path.read_text(encoding="utf-8"))

    assert meta_promoted_contract.find_missing_fields(
        bundle,
        meta_promoted_contract.PROMOTABLE_BUNDLE_REQUIRED_FIELDS,
    ) == []
    assert meta_promoted_contract.find_missing_fields(
        report,
        meta_promoted_contract.TRAINING_REPORT_REQUIRED_FIELDS,
    ) == []
    assert meta_promoted_contract.find_missing_fields(
        feature_schema,
        meta_promoted_contract.FEATURE_SCHEMA_REQUIRED_FIELDS,
    ) == []
    assert report["staged_input_contract"] == meta_promoted_contract.build_staged_input_contract()
    assert report["contract_version"] == meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION
    assert report["pipeline_version"] == meta_promoted_contract.PROMOTION_PIPELINE_VERSION
    assert report["promotion_gate"]["gate_version"] == meta_promoted_contract.PROMOTION_GATE_VERSION


def test_consumer_accepts_valid_producer_artifacts_under_shared_contract(tmp_path, monkeypatch):
    bundle_path, report_path, _ = _build_promoted_artifacts(tmp_path)

    monkeypatch.setattr(meta_gate, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
    monkeypatch.setattr(meta_gate, "_PROMOTION_REPORT_PATH", report_path)
    _reset_shadow_loader_state()

    normalized_bundle = meta_gate._get_shadow_runtime_model()
    decision = meta_gate.evaluate_runtime_decision(
        _shadow_feature_payload(),
        expected_feature_schema_version="meta_candidate_v1",
    )

    assert normalized_bundle is not None
    assert normalized_bundle["contract_version"] == meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION
    assert decision.shadow_only is True
    assert "artifact_load_failure" not in " ".join(decision.integrity_flags)


def test_consumer_rejects_shared_schema_violation(tmp_path, monkeypatch):
    bundle_path, report_path, _ = _build_promoted_artifacts(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("threshold_selection")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert meta_promoted_contract.find_missing_fields(
        report,
        meta_promoted_contract.TRAINING_REPORT_REQUIRED_FIELDS,
    ) == ["threshold_selection"]

    monkeypatch.setattr(meta_gate, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
    monkeypatch.setattr(meta_gate, "_PROMOTION_REPORT_PATH", report_path)
    _reset_shadow_loader_state()

    decision = meta_gate.evaluate_runtime_decision(
        _shadow_feature_payload(),
        expected_feature_schema_version="meta_candidate_v1",
    )

    assert decision.allow_trade is True
    assert "artifact_load_failure:missing_report_fields" in decision.integrity_flags


def test_report_contract_version_mismatch_fails_centrally_and_at_runtime(tmp_path, monkeypatch):
    bundle_path, report_path, _ = _build_promoted_artifacts(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert meta_promoted_contract.find_expected_string_mismatch(
        report,
        "contract_version",
        meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION,
    ) is None

    report["contract_version"] = "promotable_offline_v2"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert meta_promoted_contract.find_expected_string_mismatch(
        report,
        "contract_version",
        meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION,
    ) == "promotable_offline_v2"

    monkeypatch.setattr(meta_gate, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
    monkeypatch.setattr(meta_gate, "_PROMOTION_REPORT_PATH", report_path)
    _reset_shadow_loader_state()

    decision = meta_gate.evaluate_runtime_decision(
        _shadow_feature_payload(),
        expected_feature_schema_version="meta_candidate_v1",
    )

    assert decision.allow_trade is True
    assert "artifact_load_failure:report_contract_version_mismatch" in decision.integrity_flags