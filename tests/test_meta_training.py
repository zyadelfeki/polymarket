from __future__ import annotations

import csv
import json
from pathlib import Path

import joblib
import numpy as np
import pytest

from ml import meta_training
from services.calibration_observation_service import CalibrationObservationService


def _make_training_rows(total_rows: int = 24):
    rows = []
    for index in range(total_rows):
        label = 1 if index % 4 in {1, 2} else 0
        raw_yes_prob = 0.72 if label else 0.28
        calibrated_yes_prob = 0.69 if label else 0.31
        selected_side_prob = calibrated_yes_prob if index % 2 == 0 else (1.0 - calibrated_yes_prob)
        rows.append(
            {
                "candidate_id": f"cand-{index:03d}",
                "observation_id": f"obs-{index:03d}",
                "cluster_id": f"cluster-{index:03d}",
                "feature_snapshot_ts": f"2026-03-07T00:00:{index:02d}Z",
                "feature_schema_version": "meta_candidate_v1",
                "cluster_policy_version": "cluster_v1",
                "market_id": f"market-{index:03d}",
                "token_id": "token-yes",
                "market_question": f"Question {index}",
                "selected_side": "YES" if index % 2 == 0 else "NO",
                "order_id": f"ord-{index:03d}",
                "order_state": "SETTLED",
                "order_opened_at": f"2026-03-07T00:00:{index:02d}Z",
                "order_closed_at": f"2026-03-07T00:10:{index:02d}Z",
                "requested_notional": "100.00000000",
                "requested_quantity": "200.00000000",
                "filled_quantity": "200.00000000",
                "filled_price": "0.50000000",
                "fill_ratio": "1.000000",
                "min_fill_ratio": "1.0",
                "min_positive_return_bps": "0",
                "settled_pnl": "2.00000000" if label else "-1.00000000",
                "realized_return_bps": "200.000000" if label else "-100.000000",
                "profitability_label": label,
                "actual_yes_outcome": str(label),
                "eventual_yes_market_outcome": str(label),
                "training_eligibility": "pending_resolution",
                "raw_yes_prob": f"{raw_yes_prob:.2f}",
                "yes_side_raw_probability": f"{raw_yes_prob:.2f}",
                "calibrated_yes_prob": f"{calibrated_yes_prob:.2f}",
                "selected_side_prob": f"{selected_side_prob:.2f}",
                "charlie_confidence": f"{0.85 if label else 0.35:.2f}",
                "charlie_implied_prob": f"{0.52 if label else 0.48:.2f}",
                "charlie_edge": f"{0.10 if label else -0.03:.2f}",
                "spread_bps": f"{80 + index}",
                "time_to_expiry_seconds": f"{3600 - index * 10}",
                "token_price": f"{0.40 + (index % 5) * 0.01:.2f}",
                "normalized_yes_price": f"{0.41 + (index % 5) * 0.01:.2f}",
            }
        )
    return rows


def _write_validated_inputs(tmp_path: Path, rows=None):
    rows = list(rows or _make_training_rows())
    service = CalibrationObservationService(
        ledger=None,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    manifest = service.build_training_split_manifest(rows, feature_schema_version="meta_candidate_v1")

    executed_path = tmp_path / "executed_profitability.csv"
    split_manifest_path = tmp_path / "split_manifest.json"

    with open(executed_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    with open(split_manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return executed_path, split_manifest_path, manifest


class FakeBaselinePipeline:
    fit_rows = None
    predict_rows = None

    def __init__(self, steps):
        self.steps = steps

    def fit(self, X, y):
        type(self).fit_rows = len(X)
        self._mean = float(np.mean(y)) if len(y) else 0.5
        return self

    def predict_proba(self, X):
        type(self).predict_rows = len(X)
        probs = np.full((len(X), 2), 0.0, dtype=float)
        probs[:, 1] = self._mean
        probs[:, 0] = 1.0 - self._mean
        return probs


class FakePrimaryModel:
    fit_rows = None
    predict_rows = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def fit(self, X, y):
        type(self).fit_rows = len(X)
        self._mean = float(np.mean(y)) if len(y) else 0.5
        return self

    def predict_proba(self, X):
        type(self).predict_rows = len(X)
        probs = np.full((len(X), 2), 0.0, dtype=float)
        probs[:, 1] = self._mean
        probs[:, 0] = 1.0 - self._mean
        return probs


def test_baseline_fit_on_train_only(tmp_path, monkeypatch):
    executed_path, split_manifest_path, manifest = _write_validated_inputs(tmp_path)
    validated_inputs = meta_training.load_validated_split_inputs(
        str(executed_path),
        str(split_manifest_path),
    )

    monkeypatch.setattr(meta_training, "Pipeline", FakeBaselinePipeline)
    monkeypatch.setattr(meta_training, "HistGradientBoostingClassifier", FakePrimaryModel)

    meta_training.train_models_from_validated_inputs(
        validated_inputs,
        run_id="baseline-fit-test",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )

    assert FakeBaselinePipeline.fit_rows == manifest["train_row_count"]


def test_primary_fit_on_train_only(tmp_path, monkeypatch):
    executed_path, split_manifest_path, manifest = _write_validated_inputs(tmp_path)
    validated_inputs = meta_training.load_validated_split_inputs(
        str(executed_path),
        str(split_manifest_path),
    )

    monkeypatch.setattr(meta_training, "Pipeline", FakeBaselinePipeline)
    monkeypatch.setattr(meta_training, "HistGradientBoostingClassifier", FakePrimaryModel)

    meta_training.train_models_from_validated_inputs(
        validated_inputs,
        run_id="primary-fit-test",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )

    assert FakePrimaryModel.fit_rows == manifest["train_row_count"]


def test_frozen_model_scoring_on_validation(tmp_path, monkeypatch):
    executed_path, split_manifest_path, manifest = _write_validated_inputs(tmp_path)
    validated_inputs = meta_training.load_validated_split_inputs(
        str(executed_path),
        str(split_manifest_path),
    )

    monkeypatch.setattr(meta_training, "Pipeline", FakeBaselinePipeline)
    monkeypatch.setattr(meta_training, "HistGradientBoostingClassifier", FakePrimaryModel)

    trained = meta_training.train_models_from_validated_inputs(
        validated_inputs,
        run_id="validation-score-test",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )

    assert FakeBaselinePipeline.predict_rows == manifest["validation_row_count"]
    assert FakePrimaryModel.predict_rows == manifest["validation_row_count"]
    assert trained["run_metadata"]["primary_model_frozen_before_validation_scoring"] is True
    assert len(trained["run_metadata"]["validation_scoring"]["primary_scores"]) == manifest["validation_row_count"]
    assert trained["run_metadata"]["test_candidate_ids"]


def test_deterministic_artifact_generation_with_fixed_seed(tmp_path):
    executed_path, split_manifest_path, _ = _write_validated_inputs(tmp_path)
    first_output_dir = tmp_path / "staging-first"
    second_output_dir = tmp_path / "staging-second"

    first = meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(first_output_dir),
        run_id="deterministic-run",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )
    second = meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(second_output_dir),
        run_id="deterministic-run",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )

    assert first["feature_schema"] == second["feature_schema"]
    assert first["run_metadata"] == second["run_metadata"]

    first_model = joblib.load(first_output_dir / meta_training.MODEL_FILENAME)
    second_model = joblib.load(second_output_dir / meta_training.MODEL_FILENAME)
    assert first_model["feature_names"] == second_model["feature_names"]

    validated_inputs = meta_training.load_validated_split_inputs(str(executed_path), str(split_manifest_path))
    X_validation, _ = meta_training._rows_to_matrix(validated_inputs["split_rows"]["validation"])
    first_primary_scores = first_model["primary_model"].predict_proba(X_validation)[:, 1].tolist()
    second_primary_scores = second_model["primary_model"].predict_proba(X_validation)[:, 1].tolist()
    assert first_primary_scores == second_primary_scores


def test_artifact_payload_schema_validation(tmp_path):
    executed_path, split_manifest_path, manifest = _write_validated_inputs(tmp_path)
    report = meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(tmp_path / "staging"),
        run_id="schema-validation-run",
        created_at="2026-03-07T02:00:00Z",
        random_state=42,
    )

    model_payload = report["model_payload"]
    feature_schema = report["feature_schema"]
    run_metadata = report["run_metadata"]

    assert {
        "artifact_version",
        "model_version",
        "trainer_version",
        "feature_schema_version",
        "label_contract_version",
        "split_policy_version",
        "split_policy_hash",
        "random_state",
        "baseline_model_type",
        "primary_model_type",
        "feature_names",
        "feature_defaults",
        "train_row_count",
        "validation_row_count",
        "test_row_count",
        "baseline_model",
        "primary_model",
        "created_at",
    }.issubset(model_payload.keys())
    assert {
        "artifact_version",
        "feature_schema_version",
        "runtime_extractor_version",
        "feature_names_in_order",
        "required_features",
        "optional_features_with_defaults",
        "forbidden_features",
        "feature_types",
        "feature_descriptions",
        "source_dataset",
        "schema_hash",
        "created_at",
    }.issubset(feature_schema.keys())
    assert {
        "artifact_version",
        "run_id",
        "model_version",
        "trainer_version",
        "feature_schema_version",
        "label_contract_version",
        "split_policy_version",
        "split_policy_hash",
        "executed_profitability_path",
        "split_manifest_path",
        "random_state",
        "baseline_model_type",
        "primary_model_type",
        "train_row_count",
        "validation_row_count",
        "test_row_count",
        "train_cluster_count",
        "validation_cluster_count",
        "test_cluster_count",
        "train_candidate_ids",
        "validation_candidate_ids",
        "test_candidate_ids",
        "primary_model_frozen_before_validation_scoring",
        "validation_scoring",
        "feature_names",
        "feature_schema_hash",
        "created_at",
    }.issubset(run_metadata.keys())
    assert model_payload["train_row_count"] == manifest["train_row_count"]
    assert feature_schema["feature_names_in_order"] == meta_training.FEATURE_NAMES_IN_ORDER
    assert run_metadata["validation_scoring"]["candidate_ids"] == run_metadata["validation_candidate_ids"]