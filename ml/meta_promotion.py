from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    brier_score_loss,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

from ml import meta_promoted_contract, meta_training


TRAINING_REPORT_ARTIFACT_VERSION = meta_promoted_contract.TRAINING_REPORT_ARTIFACT_VERSION
PROMOTABLE_MODEL_BUNDLE_ARTIFACT_VERSION = meta_promoted_contract.PROMOTABLE_MODEL_BUNDLE_ARTIFACT_VERSION
PROMOTION_PIPELINE_VERSION = meta_promoted_contract.PROMOTION_PIPELINE_VERSION
PROMOTION_GATE_VERSION = meta_promoted_contract.PROMOTION_GATE_VERSION
STAGED_INPUT_CONTRACT_VERSION = meta_promoted_contract.STAGED_INPUT_CONTRACT_VERSION
PROMOTABLE_CONTRACT_VERSION = meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION

TRAINING_REPORT_FILENAME = meta_promoted_contract.TRAINING_REPORT_FILENAME
PROMOTABLE_MODEL_BUNDLE_FILENAME = meta_promoted_contract.PROMOTABLE_MODEL_BUNDLE_FILENAME

DEFAULT_MIN_VALIDATION_ROWS = 12
DEFAULT_MIN_TEST_ROWS = 12
DEFAULT_MIN_MINORITY_COUNT = 3
DEFAULT_MIN_MINORITY_FRACTION = 0.20
DEFAULT_MAX_CALIBRATION_BRIER_DEGRADATION = 0.02
DEFAULT_MAX_CALIBRATION_LOG_LOSS_DEGRADATION = 0.05

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACTIVE_MODEL_PATH = _REPO_ROOT / "models" / "meta_gate.pkl"
DEFAULT_ACTIVE_THRESHOLD_PATH = _REPO_ROOT / "models" / "meta_gate_threshold.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _file_fingerprint(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "size_bytes": 0,
            "content_hash": None,
        }
    payload = path.read_bytes()
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": len(payload),
        "content_hash": joblib.hash(payload),
    }


def _clip_scores(scores: List[float]) -> np.ndarray:
    return np.clip(np.asarray(scores, dtype=float), 1e-6, 1.0 - 1e-6)


def _positive_negative_means(labels: List[int], scores: List[float]) -> Dict[str, Optional[float]]:
    labels_array = np.asarray(labels, dtype=int)
    scores_array = np.asarray(scores, dtype=float)
    positive_scores = scores_array[labels_array == 1]
    negative_scores = scores_array[labels_array == 0]
    return {
        "positive_mean_score": float(np.mean(positive_scores)) if len(positive_scores) else None,
        "negative_mean_score": float(np.mean(negative_scores)) if len(negative_scores) else None,
    }


def _evaluate_probability_scores(
    labels: List[int],
    scores: List[float],
    *,
    threshold: float,
) -> Dict[str, Any]:
    labels_array = np.asarray(labels, dtype=int)
    clipped_scores = _clip_scores(scores)
    predicted = (clipped_scores >= float(threshold)).astype(int)

    tp = int(np.sum((predicted == 1) & (labels_array == 1)))
    fp = int(np.sum((predicted == 1) & (labels_array == 0)))
    tn = int(np.sum((predicted == 0) & (labels_array == 0)))
    fn = int(np.sum((predicted == 0) & (labels_array == 1)))

    positive_count = int(np.sum(labels_array == 1))
    negative_count = int(np.sum(labels_array == 0))
    positive_rate = float(np.mean(labels_array)) if len(labels_array) else 0.0
    mean_summary = _positive_negative_means(labels, clipped_scores.tolist())

    metrics = {
        "threshold": float(threshold),
        "sample_count": int(len(labels_array)),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "positive_rate": positive_rate,
        "mean_score": float(np.mean(clipped_scores)) if len(clipped_scores) else 0.0,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": float(accuracy_score(labels_array, predicted)) if len(labels_array) else 0.0,
        "balanced_accuracy": (
            float(balanced_accuracy_score(labels_array, predicted))
            if positive_count and negative_count
            else None
        ),
        "precision": float(precision_score(labels_array, predicted, zero_division=0)),
        "recall": float(recall_score(labels_array, predicted, zero_division=0)),
        "f1": float(f1_score(labels_array, predicted, zero_division=0)),
        "brier": float(brier_score_loss(labels_array, clipped_scores)) if len(labels_array) else 0.0,
        "log_loss": float(log_loss(labels_array, clipped_scores, labels=[0, 1])) if len(labels_array) else 0.0,
        "auc": (
            float(roc_auc_score(labels_array, clipped_scores))
            if positive_count and negative_count
            else None
        ),
        **mean_summary,
    }
    return metrics


def _select_threshold_from_validation(scores: List[float], labels: List[int]) -> Dict[str, Any]:
    candidate_thresholds = sorted({0.5, *[float(value) for value in scores]})
    best_choice: Optional[Dict[str, Any]] = None

    for threshold in candidate_thresholds:
        metrics = _evaluate_probability_scores(labels, scores, threshold=threshold)
        balanced_accuracy = metrics["balanced_accuracy"]
        ranking_key = (
            -1.0 if balanced_accuracy is None else float(balanced_accuracy),
            float(metrics["f1"]),
            -abs(float(threshold) - 0.5),
            -float(threshold),
        )
        candidate = {
            "threshold": float(threshold),
            "metrics": metrics,
            "ranking_key": ranking_key,
        }
        if best_choice is None or candidate["ranking_key"] > best_choice["ranking_key"]:
            best_choice = candidate

    assert best_choice is not None
    return {
        "selection_metric": "balanced_accuracy_then_f1_then_distance_to_0_5_then_lower_threshold",
        "candidate_threshold_count": len(candidate_thresholds),
        "selected_threshold": float(best_choice["threshold"]),
        "selected_metrics": dict(best_choice["metrics"]),
    }


def _apply_calibrator(calibrator, scores: List[float]) -> List[float]:
    feature_matrix = np.asarray(scores, dtype=float).reshape(-1, 1)
    calibrated = calibrator.predict_proba(feature_matrix)[:, 1].astype(float).tolist()
    return calibrated


class _IdentityCalibrator:
    def predict_proba(self, X):
        values = np.clip(np.asarray(X, dtype=float).reshape(-1), 1e-6, 1.0 - 1e-6)
        return np.column_stack([1.0 - values, values])


def _fit_validation_calibrator(
    primary_validation_scores: List[float],
    validation_labels: List[int],
    *,
    random_state: int,
) -> Dict[str, Any]:
    feature_matrix = np.asarray(primary_validation_scores, dtype=float).reshape(-1, 1)
    labels_array = np.asarray(validation_labels, dtype=int)

    if len(np.unique(labels_array)) < 2:
        calibrator = _IdentityCalibrator()
        calibrated_validation_scores = list(primary_validation_scores)
        return {
            "calibrator": calibrator,
            "calibration_method": "platt_logistic_regression_v1",
            "fit_split": meta_promoted_contract.VALIDATION_ONLY_FIT_SPLIT,
            "fit_status": "insufficient_class_diversity",
            "fit_sample_count": int(len(validation_labels)),
            "coefficient": 0.0,
            "intercept": 0.0,
            "calibrated_validation_scores": calibrated_validation_scores,
        }

    calibrator = LogisticRegression(
        solver="liblinear",
        max_iter=1000,
        random_state=int(random_state),
    )
    calibrator.fit(feature_matrix, labels_array)
    calibrated_validation_scores = _apply_calibrator(calibrator, primary_validation_scores)

    coefficient = None
    intercept = None
    if hasattr(calibrator, "coef_"):
        coefficient = float(np.asarray(calibrator.coef_).reshape(-1)[0])
    if hasattr(calibrator, "intercept_"):
        intercept = float(np.asarray(calibrator.intercept_).reshape(-1)[0])

    return {
        "calibrator": calibrator,
        "calibration_method": "platt_logistic_regression_v1",
        "fit_split": meta_promoted_contract.VALIDATION_ONLY_FIT_SPLIT,
        "fit_status": "fitted",
        "fit_sample_count": int(len(validation_labels)),
        "coefficient": coefficient,
        "intercept": intercept,
        "calibrated_validation_scores": calibrated_validation_scores,
    }


def load_staged_training_artifacts(staging_dir: str) -> Dict[str, Any]:
    staging_path = Path(staging_dir)
    model_path = staging_path / meta_training.MODEL_FILENAME
    feature_schema_path = staging_path / meta_training.FEATURE_SCHEMA_FILENAME
    run_metadata_path = staging_path / meta_training.RUN_METADATA_FILENAME

    return {
        "staging_dir": str(staging_path),
        "model_path": str(model_path),
        "feature_schema_path": str(feature_schema_path),
        "run_metadata_path": str(run_metadata_path),
        "model_payload": joblib.load(model_path),
        "feature_schema": _load_json_file(feature_schema_path),
        "run_metadata": _load_json_file(run_metadata_path),
    }


def _build_integrity_checks(
    staged_artifacts: Dict[str, Any],
    validated_inputs: Dict[str, Any],
) -> Dict[str, Any]:
    model_payload = staged_artifacts["model_payload"]
    feature_schema = staged_artifacts["feature_schema"]
    run_metadata = staged_artifacts["run_metadata"]
    manifest = validated_inputs["manifest"]
    split_rows = validated_inputs["split_rows"]

    checks: List[Dict[str, Any]] = []

    def add_check(name: str, passed: bool, *, expected: Any = None, actual: Any = None) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "expected": expected,
                "actual": actual,
            }
        )

    add_check(
        "model_version_match",
        str(model_payload.get("model_version")) == str(run_metadata.get("model_version")),
        expected=run_metadata.get("model_version"),
        actual=model_payload.get("model_version"),
    )
    add_check(
        "feature_schema_version_match",
        str(model_payload.get("feature_schema_version"))
        == str(feature_schema.get("feature_schema_version"))
        == str(run_metadata.get("feature_schema_version"))
        == str(manifest.get("feature_schema_version")),
        expected=run_metadata.get("feature_schema_version"),
        actual={
            "model": model_payload.get("feature_schema_version"),
            "feature_schema": feature_schema.get("feature_schema_version"),
            "manifest": manifest.get("feature_schema_version"),
        },
    )
    add_check(
        "feature_names_match",
        list(model_payload.get("feature_names") or [])
        == list(feature_schema.get("feature_names_in_order") or [])
        == list(run_metadata.get("feature_names") or []),
        expected=feature_schema.get("feature_names_in_order"),
        actual={
            "model": model_payload.get("feature_names"),
            "run_metadata": run_metadata.get("feature_names"),
        },
    )
    add_check(
        "feature_schema_hash_match",
        str(run_metadata.get("feature_schema_hash")) == str(feature_schema.get("schema_hash")),
        expected=feature_schema.get("schema_hash"),
        actual=run_metadata.get("feature_schema_hash"),
    )
    add_check(
        "split_policy_version_match",
        str(model_payload.get("split_policy_version"))
        == str(run_metadata.get("split_policy_version"))
        == str(manifest.get("split_policy_version")),
        expected=manifest.get("split_policy_version"),
        actual={
            "model": model_payload.get("split_policy_version"),
            "run_metadata": run_metadata.get("split_policy_version"),
        },
    )
    add_check(
        "split_policy_hash_match",
        str(model_payload.get("split_policy_hash"))
        == str(run_metadata.get("split_policy_hash"))
        == str(manifest.get("split_policy_hash")),
        expected=manifest.get("split_policy_hash"),
        actual={
            "model": model_payload.get("split_policy_hash"),
            "run_metadata": run_metadata.get("split_policy_hash"),
        },
    )
    add_check(
        "validation_candidate_ids_match",
        list(run_metadata.get("validation_candidate_ids") or [])
        == [str(row["candidate_id"]) for row in split_rows["validation"]]
        == list((run_metadata.get("validation_scoring") or {}).get("candidate_ids") or []),
        expected=[str(row["candidate_id"]) for row in split_rows["validation"]],
        actual={
            "run_metadata": run_metadata.get("validation_candidate_ids"),
            "validation_scoring": (run_metadata.get("validation_scoring") or {}).get("candidate_ids"),
        },
    )
    add_check(
        "train_row_count_match",
        int(model_payload.get("train_row_count") or 0)
        == int(run_metadata.get("train_row_count") or 0)
        == len(split_rows["train"]),
        expected=len(split_rows["train"]),
        actual={
            "model": model_payload.get("train_row_count"),
            "run_metadata": run_metadata.get("train_row_count"),
        },
    )
    add_check(
        "validation_row_count_match",
        int(model_payload.get("validation_row_count") or 0)
        == int(run_metadata.get("validation_row_count") or 0)
        == len(split_rows["validation"]),
        expected=len(split_rows["validation"]),
        actual={
            "model": model_payload.get("validation_row_count"),
            "run_metadata": run_metadata.get("validation_row_count"),
        },
    )
    add_check(
        "test_row_count_match",
        int(model_payload.get("test_row_count") or 0)
        == int(run_metadata.get("test_row_count") or 0)
        == len(split_rows["test"]),
        expected=len(split_rows["test"]),
        actual={
            "model": model_payload.get("test_row_count"),
            "run_metadata": run_metadata.get("test_row_count"),
        },
    )

    errors = [check["name"] for check in checks if not check["passed"]]
    return {
        "checks": checks,
        "passed": not errors,
        "errors": errors,
    }


def _minority_fraction(metrics: Dict[str, Any]) -> float:
    sample_count = int(metrics.get("sample_count") or 0)
    if sample_count <= 0:
        return 0.0
    minority_count = min(int(metrics.get("positive_count") or 0), int(metrics.get("negative_count") or 0))
    return float(minority_count / sample_count)


def _evaluate_promotion_gate(
    *,
    integrity: Dict[str, Any],
    validation_primary_metrics: Dict[str, Any],
    validation_calibrated_metrics: Dict[str, Any],
    calibration_fit: Dict[str, Any],
    test_calibrated_metrics: Dict[str, Any],
) -> Dict[str, Any]:
    reasons: List[Dict[str, Any]] = []

    def add_reason(code: str, message: str, **context: Any) -> None:
        reasons.append({"code": code, "message": message, "context": context})

    if not integrity.get("passed", False):
        add_reason(
            "artifact_cross_reference_mismatch",
            "Staged artifacts failed cross-reference validation.",
            failed_checks=list(integrity.get("errors") or []),
        )

    validation_minority_count = min(
        int(validation_calibrated_metrics.get("positive_count") or 0),
        int(validation_calibrated_metrics.get("negative_count") or 0),
    )
    test_minority_count = min(
        int(test_calibrated_metrics.get("positive_count") or 0),
        int(test_calibrated_metrics.get("negative_count") or 0),
    )

    if int(validation_calibrated_metrics.get("sample_count") or 0) < DEFAULT_MIN_VALIDATION_ROWS or int(
        test_calibrated_metrics.get("sample_count") or 0
    ) < DEFAULT_MIN_TEST_ROWS:
        add_reason(
            "low_sample_size",
            "Validation/test sample count is below the promotion minimum.",
            min_validation_rows=DEFAULT_MIN_VALIDATION_ROWS,
            min_test_rows=DEFAULT_MIN_TEST_ROWS,
            validation_sample_count=validation_calibrated_metrics.get("sample_count"),
            test_sample_count=test_calibrated_metrics.get("sample_count"),
        )

    if (
        validation_minority_count < DEFAULT_MIN_MINORITY_COUNT
        or test_minority_count < DEFAULT_MIN_MINORITY_COUNT
        or _minority_fraction(validation_calibrated_metrics) < DEFAULT_MIN_MINORITY_FRACTION
        or _minority_fraction(test_calibrated_metrics) < DEFAULT_MIN_MINORITY_FRACTION
    ):
        add_reason(
            "class_imbalance",
            "Validation/test label balance is below the promotion minimum.",
            min_minority_count=DEFAULT_MIN_MINORITY_COUNT,
            min_minority_fraction=DEFAULT_MIN_MINORITY_FRACTION,
            validation_minority_count=validation_minority_count,
            test_minority_count=test_minority_count,
            validation_minority_fraction=_minority_fraction(validation_calibrated_metrics),
            test_minority_fraction=_minority_fraction(test_calibrated_metrics),
        )

    if (
        calibration_fit.get("coefficient") is not None and float(calibration_fit["coefficient"]) <= 0.0
    ) or (
        validation_calibrated_metrics.get("positive_mean_score") is not None
        and validation_calibrated_metrics.get("negative_mean_score") is not None
        and float(validation_calibrated_metrics["positive_mean_score"])
        <= float(validation_calibrated_metrics["negative_mean_score"])
    ) or (
        test_calibrated_metrics.get("auc") is not None and float(test_calibrated_metrics["auc"]) < 0.5
    ):
        add_reason(
            "score_inversion",
            "Calibrated scores are inverted or non-separating.",
            calibration_coefficient=calibration_fit.get("coefficient"),
            validation_positive_mean_score=validation_calibrated_metrics.get("positive_mean_score"),
            validation_negative_mean_score=validation_calibrated_metrics.get("negative_mean_score"),
            test_auc=test_calibrated_metrics.get("auc"),
        )

    if float(validation_calibrated_metrics.get("brier") or 0.0) > float(
        validation_primary_metrics.get("brier") or 0.0
    ) + DEFAULT_MAX_CALIBRATION_BRIER_DEGRADATION or float(
        validation_calibrated_metrics.get("log_loss") or 0.0
    ) > float(validation_primary_metrics.get("log_loss") or 0.0) + DEFAULT_MAX_CALIBRATION_LOG_LOSS_DEGRADATION:
        add_reason(
            "bad_calibration",
            "Calibration degraded validation probability quality.",
            validation_primary_brier=validation_primary_metrics.get("brier"),
            validation_calibrated_brier=validation_calibrated_metrics.get("brier"),
            validation_primary_log_loss=validation_primary_metrics.get("log_loss"),
            validation_calibrated_log_loss=validation_calibrated_metrics.get("log_loss"),
        )

    return {
        "artifact_version": TRAINING_REPORT_ARTIFACT_VERSION,
        "gate_version": PROMOTION_GATE_VERSION,
        "passed": not reasons,
        "blocked": bool(reasons),
        "reasons": reasons,
    }


def finalize_staged_training(
    *,
    staging_dir: str,
    output_dir: str,
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
    random_state: int = meta_training.DEFAULT_RANDOM_STATE,
    active_model_path: Optional[str] = None,
    active_threshold_path: Optional[str] = None,
) -> Dict[str, Any]:
    staged_artifacts = load_staged_training_artifacts(staging_dir)
    model_payload = staged_artifacts["model_payload"]
    feature_schema = staged_artifacts["feature_schema"]
    run_metadata = staged_artifacts["run_metadata"]

    validated_inputs = meta_training.load_validated_split_inputs(
        executed_profitability_path=str(run_metadata["executed_profitability_path"]),
        split_manifest_path=str(run_metadata["split_manifest_path"]),
    )
    manifest = validated_inputs["manifest"]
    split_rows = validated_inputs["split_rows"]

    created_at_value = str(created_at or _utc_now_iso())
    run_id_value = str(run_id or f"meta_promote_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    active_model_target = Path(active_model_path) if active_model_path else DEFAULT_ACTIVE_MODEL_PATH
    active_threshold_target = Path(active_threshold_path) if active_threshold_path else DEFAULT_ACTIVE_THRESHOLD_PATH
    active_before = {
        "model": _file_fingerprint(active_model_target),
        "threshold": _file_fingerprint(active_threshold_target),
    }

    integrity = _build_integrity_checks(staged_artifacts, validated_inputs)
    validation_scoring = dict(run_metadata.get("validation_scoring") or {})
    validation_labels = [int(value) for value in validation_scoring.get("labels") or []]
    baseline_validation_scores = [float(value) for value in validation_scoring.get("baseline_scores") or []]
    primary_validation_scores = [float(value) for value in validation_scoring.get("primary_scores") or []]

    baseline_validation_metrics = _evaluate_probability_scores(
        validation_labels,
        baseline_validation_scores,
        threshold=0.5,
    )
    primary_validation_metrics = _evaluate_probability_scores(
        validation_labels,
        primary_validation_scores,
        threshold=0.5,
    )

    calibration_fit = _fit_validation_calibrator(
        primary_validation_scores,
        validation_labels,
        random_state=int(random_state),
    )
    calibrated_validation_scores = list(calibration_fit["calibrated_validation_scores"])
    calibrated_validation_metrics = _evaluate_probability_scores(
        validation_labels,
        calibrated_validation_scores,
        threshold=0.5,
    )
    threshold_selection = _select_threshold_from_validation(
        calibrated_validation_scores,
        validation_labels,
    )
    selected_threshold = float(threshold_selection["selected_threshold"])

    baseline_model = model_payload["baseline_model"]
    primary_model = model_payload["primary_model"]
    X_test, y_test_array = meta_training._rows_to_matrix(split_rows["test"])
    y_test = y_test_array.astype(int).tolist()

    threshold_selected_before_test_evaluation = True
    baseline_test_scores = baseline_model.predict_proba(X_test)[:, 1].astype(float).tolist()
    primary_test_scores = primary_model.predict_proba(X_test)[:, 1].astype(float).tolist()
    calibrated_test_scores = _apply_calibrator(calibration_fit["calibrator"], primary_test_scores)

    baseline_test_metrics = _evaluate_probability_scores(y_test, baseline_test_scores, threshold=0.5)
    primary_test_metrics = _evaluate_probability_scores(y_test, primary_test_scores, threshold=0.5)
    calibrated_test_metrics = _evaluate_probability_scores(
        y_test,
        calibrated_test_scores,
        threshold=selected_threshold,
    )

    promotion_gate = _evaluate_promotion_gate(
        integrity=integrity,
        validation_primary_metrics=primary_validation_metrics,
        validation_calibrated_metrics=calibrated_validation_metrics,
        calibration_fit=calibration_fit,
        test_calibrated_metrics=calibrated_test_metrics,
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    training_report_path = output_path / TRAINING_REPORT_FILENAME
    promotable_bundle_path = output_path / PROMOTABLE_MODEL_BUNDLE_FILENAME

    final_bundle_path: Optional[str] = None
    if promotion_gate["passed"]:
        promotable_bundle = {
            "artifact_version": PROMOTABLE_MODEL_BUNDLE_ARTIFACT_VERSION,
            "contract_version": PROMOTABLE_CONTRACT_VERSION,
            "pipeline_version": PROMOTION_PIPELINE_VERSION,
            "run_id": run_id_value,
            "created_at": created_at_value,
            "model_version": str(model_payload["model_version"]),
            "feature_schema_version": str(feature_schema["feature_schema_version"]),
            "label_contract_version": str(model_payload["label_contract_version"]),
            "split_policy_version": str(model_payload["split_policy_version"]),
            "split_policy_hash": str(model_payload["split_policy_hash"]),
            "baseline_model": baseline_model,
            "primary_model": primary_model,
            "calibrator": calibration_fit["calibrator"],
            "selected_threshold": selected_threshold,
            "feature_names": list(model_payload["feature_names"]),
            "feature_defaults": dict(model_payload["feature_defaults"]),
            "staged_model_path": str(staged_artifacts["model_path"]),
            "staged_feature_schema_path": str(staged_artifacts["feature_schema_path"]),
            "staged_run_metadata_path": str(staged_artifacts["run_metadata_path"]),
            "training_report_path": str(training_report_path),
        }
        meta_promoted_contract.ensure_required_fields(
            promotable_bundle,
            meta_promoted_contract.PROMOTABLE_BUNDLE_REQUIRED_FIELDS,
            artifact_name="promotable bundle",
        )
        joblib.dump(promotable_bundle, promotable_bundle_path)
        final_bundle_path = str(promotable_bundle_path)

    active_after = {
        "model": _file_fingerprint(active_model_target),
        "threshold": _file_fingerprint(active_threshold_target),
    }

    training_report = {
        "artifact_version": TRAINING_REPORT_ARTIFACT_VERSION,
        "contract_version": PROMOTABLE_CONTRACT_VERSION,
        "pipeline_version": PROMOTION_PIPELINE_VERSION,
        "run_id": run_id_value,
        "created_at": created_at_value,
        "staged_input_contract": meta_promoted_contract.build_staged_input_contract(),
        "staged_artifacts": {
            "staging_dir": str(staged_artifacts["staging_dir"]),
            "model_path": str(staged_artifacts["model_path"]),
            "feature_schema_path": str(staged_artifacts["feature_schema_path"]),
            "run_metadata_path": str(staged_artifacts["run_metadata_path"]),
            "model_version": str(model_payload["model_version"]),
            "feature_schema_version": str(feature_schema["feature_schema_version"]),
            "feature_schema_hash": str(feature_schema["schema_hash"]),
            "label_contract_version": str(model_payload["label_contract_version"]),
            "split_policy_version": str(model_payload["split_policy_version"]),
            "split_policy_hash": str(model_payload["split_policy_hash"]),
        },
        "validated_inputs": {
            "executed_profitability_path": str(validated_inputs["executed_profitability_path"]),
            "split_manifest_path": str(validated_inputs["split_manifest_path"]),
            "train_row_count": len(split_rows["train"]),
            "validation_row_count": len(split_rows["validation"]),
            "test_row_count": len(split_rows["test"]),
            "train_time_start": manifest.get("train_time_start"),
            "train_time_end": manifest.get("train_time_end"),
            "validation_time_start": manifest.get("validation_time_start"),
            "validation_time_end": manifest.get("validation_time_end"),
            "test_time_start": manifest.get("test_time_start"),
            "test_time_end": manifest.get("test_time_end"),
        },
        "integrity": integrity,
        "calibration": {
            "method": calibration_fit["calibration_method"],
            "fit_split": calibration_fit["fit_split"],
            "fit_status": calibration_fit.get("fit_status"),
            "fit_sample_count": calibration_fit["fit_sample_count"],
            "coefficient": calibration_fit["coefficient"],
            "intercept": calibration_fit["intercept"],
            "validation_primary_metrics": primary_validation_metrics,
            "validation_calibrated_metrics": calibrated_validation_metrics,
        },
        "threshold_selection": {
            **threshold_selection,
            "selected_before_test_evaluation": threshold_selected_before_test_evaluation,
        },
        "evaluation": {
            "validation": {
                "baseline": baseline_validation_metrics,
                "primary": primary_validation_metrics,
                "calibrated": calibrated_validation_metrics,
            },
            "test": {
                "baseline": baseline_test_metrics,
                "primary": primary_test_metrics,
                "calibrated": calibrated_test_metrics,
                "candidate_ids": [str(row["candidate_id"]) for row in split_rows["test"]],
            },
        },
        "test_evaluation": {
            "test_split_touched_once": True,
            "baseline_predict_proba_calls": 1,
            "primary_predict_proba_calls": 1,
        },
        "promotion_gate": promotion_gate,
        "outputs": {
            "training_report_path": str(training_report_path),
            "promotable_model_bundle_path": final_bundle_path,
        },
        "active_runtime_paths": {
            "model_path": str(active_model_target),
            "threshold_override_path": str(active_threshold_target),
            "preserved": active_before == active_after,
            "before": active_before,
            "after": active_after,
        },
    }
    meta_promoted_contract.ensure_required_fields(
        training_report,
        meta_promoted_contract.TRAINING_REPORT_REQUIRED_FIELDS,
        artifact_name="training report",
    )
    _write_json_file(training_report_path, training_report)

    return {
        "training_report_path": str(training_report_path),
        "promotable_model_bundle_path": final_bundle_path,
        "training_report": training_report,
        "promotion_gate": promotion_gate,
    }