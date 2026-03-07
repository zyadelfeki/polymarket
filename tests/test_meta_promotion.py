from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import pytest

from ml import meta_promotion, meta_training
from tests.test_meta_training import _make_training_rows, _write_validated_inputs


def _stage_training_artifacts(tmp_path: Path, *, total_rows: int = 120, rows=None) -> tuple[Path, Path, dict, Path]:
    rows = list(rows or _make_training_rows(total_rows))
    executed_path, split_manifest_path, manifest = _write_validated_inputs(tmp_path, rows=rows)
    staging_dir = tmp_path / "staging"
    meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(staging_dir),
        run_id="ticket-4-3-stage",
        created_at="2026-03-07T03:00:00Z",
        random_state=42,
    )
    return executed_path, split_manifest_path, manifest, staging_dir


class RecordingCalibrator:
    fit_values = None
    fit_labels = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.coef_ = np.array([[1.0]], dtype=float)
        self.intercept_ = np.array([0.0], dtype=float)

    def fit(self, X, y):
        type(self).fit_values = X.reshape(-1).tolist()
        type(self).fit_labels = np.asarray(y, dtype=int).tolist()
        return self

    def predict_proba(self, X):
        values = np.clip(np.asarray(X, dtype=float).reshape(-1), 1e-6, 1.0 - 1e-6)
        return np.column_stack([1.0 - values, values])


class ThresholdOrderModel:
    threshold_selected = False
    predict_calls = 0

    def predict_proba(self, X):
        assert type(self).threshold_selected is True
        type(self).predict_calls += 1
        values = np.linspace(0.2, 0.8, num=len(X), dtype=float)
        return np.column_stack([1.0 - values, values])


class CountingTestModel:
    predict_calls = 0

    def predict_proba(self, X):
        type(self).predict_calls += 1
        values = np.linspace(0.2, 0.8, num=len(X), dtype=float)
        return np.column_stack([1.0 - values, values])


def test_calibration_fit_on_validation_scores_only(tmp_path, monkeypatch):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)
    run_metadata = json.loads((staging_dir / meta_training.RUN_METADATA_FILENAME).read_text(encoding="utf-8"))

    monkeypatch.setattr(meta_promotion, "LogisticRegression", RecordingCalibrator)
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="calibration-validation-only",
        created_at="2026-03-07T04:00:00Z",
    )

    assert RecordingCalibrator.fit_values == run_metadata["validation_scoring"]["primary_scores"]
    assert RecordingCalibrator.fit_labels == run_metadata["validation_scoring"]["labels"]
    assert report["training_report"]["calibration"]["fit_split"] == "validation_only"


def test_threshold_chosen_before_test_evaluation(tmp_path, monkeypatch):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)
    model_path = staging_dir / meta_training.MODEL_FILENAME
    model_payload = joblib.load(model_path)
    ThresholdOrderModel.threshold_selected = False
    ThresholdOrderModel.predict_calls = 0
    model_payload["baseline_model"] = ThresholdOrderModel()
    model_payload["primary_model"] = ThresholdOrderModel()
    joblib.dump(model_payload, model_path)

    original_selector = meta_promotion._select_threshold_from_validation

    def wrapped_selector(scores, labels):
        ThresholdOrderModel.threshold_selected = True
        return original_selector(scores, labels)

    monkeypatch.setattr(meta_promotion, "_select_threshold_from_validation", wrapped_selector)
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="threshold-before-test",
        created_at="2026-03-07T04:00:00Z",
    )

    assert ThresholdOrderModel.predict_calls == 2
    assert report["training_report"]["threshold_selection"]["selected_before_test_evaluation"] is True


def test_test_touched_exactly_once(tmp_path):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)
    model_path = staging_dir / meta_training.MODEL_FILENAME
    model_payload = joblib.load(model_path)
    CountingTestModel.predict_calls = 0
    model_payload["baseline_model"] = CountingTestModel()
    model_payload["primary_model"] = CountingTestModel()
    joblib.dump(model_payload, model_path)

    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="test-touch-once",
        created_at="2026-03-07T04:00:00Z",
    )

    assert CountingTestModel.predict_calls == 2
    assert report["training_report"]["test_evaluation"]["test_split_touched_once"] is True
    assert report["training_report"]["test_evaluation"]["baseline_predict_proba_calls"] == 1
    assert report["training_report"]["test_evaluation"]["primary_predict_proba_calls"] == 1


def test_promotion_blocked_on_low_sample_size(tmp_path):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=24)
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="low-sample",
        created_at="2026-03-07T04:00:00Z",
    )

    codes = [reason["code"] for reason in report["promotion_gate"]["reasons"]]
    assert report["promotion_gate"]["passed"] is False
    assert "low_sample_size" in codes


def test_promotion_blocked_on_imbalance(tmp_path):
    rows = _make_training_rows(120)
    for index, row in enumerate(rows):
        row["profitability_label"] = 1 if index < 114 else 0
        row["actual_yes_outcome"] = str(row["profitability_label"])
        row["eventual_yes_market_outcome"] = str(row["profitability_label"])
        row["settled_pnl"] = "2.00000000" if row["profitability_label"] else "-1.00000000"
        row["realized_return_bps"] = "200.000000" if row["profitability_label"] else "-100.000000"
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, rows=rows)

    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="imbalance",
        created_at="2026-03-07T04:00:00Z",
    )

    codes = [reason["code"] for reason in report["promotion_gate"]["reasons"]]
    assert "class_imbalance" in codes


def test_promotion_blocked_on_inversion(tmp_path):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)
    run_metadata_path = staging_dir / meta_training.RUN_METADATA_FILENAME
    run_metadata = json.loads(run_metadata_path.read_text(encoding="utf-8"))
    labels = list(run_metadata["validation_scoring"]["labels"])
    run_metadata["validation_scoring"]["primary_scores"] = [0.99 if label == 0 else 0.01 for label in labels]
    run_metadata_path.write_text(json.dumps(run_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="inversion",
        created_at="2026-03-07T04:00:00Z",
    )

    codes = [reason["code"] for reason in report["promotion_gate"]["reasons"]]
    assert "score_inversion" in codes


def test_promotion_blocked_on_bad_calibration(tmp_path, monkeypatch):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)

    class FlatCalibrator:
        def predict_proba(self, X):
            values = np.full(len(X), 0.5, dtype=float)
            return np.column_stack([1.0 - values, values])

    def bad_fit(primary_validation_scores, validation_labels, *, random_state):
        return {
            "calibrator": FlatCalibrator(),
            "calibration_method": "platt_logistic_regression_v1",
            "fit_split": "validation_only",
            "fit_sample_count": len(validation_labels),
            "coefficient": 1.0,
            "intercept": 0.0,
            "calibrated_validation_scores": [0.5 for _ in validation_labels],
        }

    monkeypatch.setattr(meta_promotion, "_fit_validation_calibrator", bad_fit)
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="bad-calibration",
        created_at="2026-03-07T04:00:00Z",
    )

    codes = [reason["code"] for reason in report["promotion_gate"]["reasons"]]
    assert "bad_calibration" in codes


def test_promotion_blocked_on_artifact_cross_reference_mismatch(tmp_path):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=120)
    feature_schema_path = staging_dir / meta_training.FEATURE_SCHEMA_FILENAME
    feature_schema = json.loads(feature_schema_path.read_text(encoding="utf-8"))
    feature_schema["schema_hash"] = "corrupted-schema-hash"
    feature_schema_path.write_text(json.dumps(feature_schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="cross-reference-mismatch",
        created_at="2026-03-07T04:00:00Z",
    )

    codes = [reason["code"] for reason in report["promotion_gate"]["reasons"]]
    assert "artifact_cross_reference_mismatch" in codes


def test_failed_runs_do_not_overwrite_active_runtime_artifact_paths(tmp_path):
    _, _, _, staging_dir = _stage_training_artifacts(tmp_path, total_rows=24)
    active_dir = tmp_path / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    active_model_path = active_dir / "meta_gate.pkl"
    active_threshold_path = active_dir / "meta_gate_threshold.json"
    active_model_path.write_bytes(b"sentinel-model")
    active_threshold_path.write_text('{"threshold": 0.91}\n', encoding="utf-8")
    model_before = active_model_path.read_bytes()
    threshold_before = active_threshold_path.read_text(encoding="utf-8")

    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(tmp_path / "final"),
        run_id="preserve-active-paths",
        created_at="2026-03-07T04:00:00Z",
        active_model_path=str(active_model_path),
        active_threshold_path=str(active_threshold_path),
    )

    assert active_model_path.read_bytes() == model_before
    assert active_threshold_path.read_text(encoding="utf-8") == threshold_before
    assert report["training_report"]["active_runtime_paths"]["preserved"] is True


def test_end_to_end_offline_runner_from_validated_inputs_to_training_report(tmp_path):
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
    inputs_dir.mkdir(parents=True, exist_ok=True)
    executed_path, split_manifest_path, _ = _write_validated_inputs(inputs_dir, rows=rows)
    staging_dir = tmp_path / "staging"
    final_dir = tmp_path / "final"
    repo_root = Path(__file__).resolve().parent.parent

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
            "runner-stage",
            "--created-at",
            "2026-03-07T05:00:00Z",
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
            "runner-final",
            "--created-at",
            "2026-03-07T05:10:00Z",
        ],
        check=True,
        cwd=repo_root,
    )

    training_report = json.loads((final_dir / meta_promotion.TRAINING_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert training_report["promotion_gate"]["passed"] is True
    assert training_report["staged_artifacts"]["model_version"] == "meta_gate_v1_runner-stage"
    assert training_report["staged_artifacts"]["feature_schema_version"] == "meta_candidate_v1"
    assert training_report["integrity"]["passed"] is True
    assert training_report["outputs"]["promotable_model_bundle_path"] is not None
    assert training_report["threshold_selection"]["selected_before_test_evaluation"] is True
    assert training_report["evaluation"]["test"]["calibrated"]["sample_count"] == 18