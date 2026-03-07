from __future__ import annotations

import csv
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from services.calibration_observation_service import (
    CalibrationObservationService,
    META_TRAINING_LABEL_CONTRACT_VERSION,
    META_TRAINING_PURGE_POLICY,
    META_TRAINING_SORT_KEY,
    META_TRAINING_SPLIT_POLICY_VERSION,
)


MODEL_ARTIFACT_VERSION = 1
FEATURE_SCHEMA_ARTIFACT_VERSION = 1
RUN_METADATA_ARTIFACT_VERSION = 1
TRAINER_VERSION = "meta_training_v1"
DEFAULT_RANDOM_STATE = 42

MODEL_FILENAME = "model.joblib"
FEATURE_SCHEMA_FILENAME = "feature_schema.json"
RUN_METADATA_FILENAME = "training_run_metadata.json"

FEATURE_NAMES_IN_ORDER = [
    "selected_side_is_yes",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "charlie_confidence",
    "charlie_implied_prob",
    "charlie_edge",
    "spread_bps",
    "time_to_expiry_seconds",
    "token_price",
    "normalized_yes_price",
]

REQUIRED_FEATURE_FIELDS = [
    "selected_side",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "charlie_confidence",
    "charlie_implied_prob",
    "charlie_edge",
    "token_price",
    "normalized_yes_price",
]

OPTIONAL_FEATURE_DEFAULTS = {
    "spread_bps": 0.0,
    "time_to_expiry_seconds": 0.0,
}

FORBIDDEN_FEATURE_FIELDS = [
    "candidate_id",
    "observation_id",
    "cluster_id",
    "feature_snapshot_ts",
    "market_id",
    "token_id",
    "market_question",
    "order_id",
    "order_state",
    "order_opened_at",
    "order_closed_at",
    "requested_notional",
    "requested_quantity",
    "filled_quantity",
    "filled_price",
    "fill_ratio",
    "min_fill_ratio",
    "min_positive_return_bps",
    "settled_pnl",
    "realized_return_bps",
    "profitability_label",
    "actual_yes_outcome",
    "eventual_yes_market_outcome",
    "training_eligibility",
]

FEATURE_DESCRIPTIONS = {
    "selected_side_is_yes": "Binary indicator that the executed side is YES.",
    "raw_yes_prob": "Charlie raw YES-side probability.",
    "yes_side_raw_probability": "Normalized YES-side raw probability used by downstream training.",
    "calibrated_yes_prob": "Charlie calibrated YES-side probability.",
    "selected_side_prob": "Probability assigned to the actually selected trading side.",
    "charlie_confidence": "Charlie confidence score.",
    "charlie_implied_prob": "Implied market probability observed at decision time.",
    "charlie_edge": "Charlie edge estimate before meta-model training.",
    "spread_bps": "Market spread in basis points at snapshot time.",
    "time_to_expiry_seconds": "Time remaining until market expiry.",
    "token_price": "Executed token price on selected side.",
    "normalized_yes_price": "YES-equivalent price for the opportunity.",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any, *, default: float = 0.0) -> float:
    if value in {None, ""}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_csv_rows(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _resolve_cluster_ids_ref(ref: Dict[str, Any], manifest_path: Path) -> List[str]:
    storage = str(ref.get("storage") or "embedded")
    if storage == "embedded":
        return [str(value) for value in ref.get("cluster_ids", [])]
    if storage == "path":
        cluster_ref_path = manifest_path.parent / str(ref.get("path") or "")
        payload = _load_json_file(cluster_ref_path)
        return [str(value) for value in payload.get("cluster_ids", [])]
    raise ValueError(f"unsupported cluster ref storage: {storage}")


def _extract_feature_vector(row: Dict[str, Any]) -> Dict[str, float]:
    for field in REQUIRED_FEATURE_FIELDS:
        if str(row.get(field) or "").strip() == "":
            raise ValueError(f"missing required feature field: {field}")
    selected_side = str(row.get("selected_side") or "").upper()
    if selected_side not in {"YES", "NO"}:
        raise ValueError(f"unsupported selected_side for training: {selected_side}")

    return {
        "selected_side_is_yes": 1.0 if selected_side == "YES" else 0.0,
        "raw_yes_prob": _safe_float(row.get("raw_yes_prob")),
        "yes_side_raw_probability": _safe_float(row.get("yes_side_raw_probability")),
        "calibrated_yes_prob": _safe_float(row.get("calibrated_yes_prob")),
        "selected_side_prob": _safe_float(row.get("selected_side_prob")),
        "charlie_confidence": _safe_float(row.get("charlie_confidence")),
        "charlie_implied_prob": _safe_float(row.get("charlie_implied_prob")),
        "charlie_edge": _safe_float(row.get("charlie_edge")),
        "spread_bps": _safe_float(row.get("spread_bps"), default=OPTIONAL_FEATURE_DEFAULTS["spread_bps"]),
        "time_to_expiry_seconds": _safe_float(
            row.get("time_to_expiry_seconds"),
            default=OPTIONAL_FEATURE_DEFAULTS["time_to_expiry_seconds"],
        ),
        "token_price": _safe_float(row.get("token_price")),
        "normalized_yes_price": _safe_float(row.get("normalized_yes_price")),
    }


def _rows_to_matrix(rows: List[Dict[str, Any]]) -> Tuple[np.ndarray, np.ndarray]:
    feature_rows = [_extract_feature_vector(row) for row in rows]
    matrix = np.array(
        [[feature_row[name] for name in FEATURE_NAMES_IN_ORDER] for feature_row in feature_rows],
        dtype=np.float64,
    )
    labels = np.array([int(row["profitability_label"]) for row in rows], dtype=np.int64)
    return matrix, labels


def _feature_schema_hash() -> str:
    payload = {
        "feature_names_in_order": FEATURE_NAMES_IN_ORDER,
        "required_features": REQUIRED_FEATURE_FIELDS,
        "optional_features_with_defaults": OPTIONAL_FEATURE_DEFAULTS,
        "forbidden_features": FORBIDDEN_FEATURE_FIELDS,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_feature_schema_artifact(
    *,
    feature_schema_version: str,
    executed_profitability_path: str,
    created_at: str,
) -> Dict[str, Any]:
    return {
        "artifact_version": FEATURE_SCHEMA_ARTIFACT_VERSION,
        "feature_schema_version": str(feature_schema_version),
        "runtime_extractor_version": TRAINER_VERSION,
        "feature_names_in_order": list(FEATURE_NAMES_IN_ORDER),
        "required_features": list(REQUIRED_FEATURE_FIELDS),
        "optional_features_with_defaults": dict(OPTIONAL_FEATURE_DEFAULTS),
        "forbidden_features": list(FORBIDDEN_FEATURE_FIELDS),
        "feature_types": {name: "float" for name in FEATURE_NAMES_IN_ORDER},
        "feature_descriptions": dict(FEATURE_DESCRIPTIONS),
        "source_dataset": str(executed_profitability_path),
        "schema_hash": _feature_schema_hash(),
        "created_at": created_at,
    }


def load_validated_split_inputs(
    executed_profitability_path: str,
    split_manifest_path: str,
) -> Dict[str, Any]:
    executed_path = Path(executed_profitability_path)
    manifest_path = Path(split_manifest_path)
    rows = CalibrationObservationService._validate_training_input_rows(_load_csv_rows(executed_path))
    manifest = _load_json_file(manifest_path)

    expected_hash = CalibrationObservationService._build_split_policy_hash()
    if str(manifest.get("split_policy_version") or "") != META_TRAINING_SPLIT_POLICY_VERSION:
        raise ValueError("unsupported split_policy_version for Ticket 4.2 inputs")
    if str(manifest.get("split_policy_hash") or "") != expected_hash:
        raise ValueError("split_policy_hash mismatch for validated split inputs")
    if str(manifest.get("purge_policy") or "") != META_TRAINING_PURGE_POLICY:
        raise ValueError("unexpected purge policy for Ticket 4.2 inputs")

    rows_by_cluster = {str(row["cluster_id"]): row for row in rows}
    split_cluster_ids = {
        "train": _resolve_cluster_ids_ref(dict(manifest.get("train_cluster_ids_ref") or {}), manifest_path),
        "validation": _resolve_cluster_ids_ref(dict(manifest.get("validation_cluster_ids_ref") or {}), manifest_path),
        "test": _resolve_cluster_ids_ref(dict(manifest.get("test_cluster_ids_ref") or {}), manifest_path),
    }
    all_split_clusters = split_cluster_ids["train"] + split_cluster_ids["validation"] + split_cluster_ids["test"]
    if len(all_split_clusters) != len(set(all_split_clusters)):
        raise ValueError("split manifest cluster ids are not exclusive across splits")

    split_rows: Dict[str, List[Dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    for split_name, cluster_ids in split_cluster_ids.items():
        for cluster_id in cluster_ids:
            if cluster_id not in rows_by_cluster:
                raise ValueError(f"cluster_id from split manifest missing in executed_profitability: {cluster_id}")
            split_rows[split_name].append(rows_by_cluster[cluster_id])

    unassigned_clusters = sorted(set(rows_by_cluster.keys()) - set(all_split_clusters))
    if unassigned_clusters:
        raise ValueError(f"unassigned cluster ids in executed_profitability: {', '.join(unassigned_clusters)}")

    row_count_fields = {
        "train": "train_row_count",
        "validation": "validation_row_count",
        "test": "test_row_count",
    }
    cluster_count_fields = {
        "train": "train_cluster_count",
        "validation": "validation_cluster_count",
        "test": "test_cluster_count",
    }
    for split_name in ["train", "validation", "test"]:
        if len(split_rows[split_name]) != int(manifest.get(row_count_fields[split_name]) or 0):
            raise ValueError(f"split manifest row count mismatch for {split_name}")
        if len(split_cluster_ids[split_name]) != int(manifest.get(cluster_count_fields[split_name]) or 0):
            raise ValueError(f"split manifest cluster count mismatch for {split_name}")
        split_rows[split_name] = sorted(split_rows[split_name], key=CalibrationObservationService._training_sort_key)

    return {
        "manifest": manifest,
        "executed_profitability_path": str(executed_path),
        "split_manifest_path": str(manifest_path),
        "split_rows": split_rows,
    }


def train_models_from_validated_inputs(
    validated_inputs: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> Dict[str, Any]:
    manifest = dict(validated_inputs["manifest"])
    split_rows = dict(validated_inputs["split_rows"])
    executed_profitability_path = str(validated_inputs["executed_profitability_path"])
    split_manifest_path = str(validated_inputs["split_manifest_path"])

    created_at_value = str(created_at or _utc_now_iso())
    run_id_value = str(run_id or f"meta_train_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    model_version = f"meta_gate_v1_{run_id_value}"
    feature_schema_version = str(manifest.get("feature_schema_version") or "meta_candidate_v1")

    X_train, y_train = _rows_to_matrix(split_rows["train"])
    X_validation, y_validation = _rows_to_matrix(split_rows["validation"])

    baseline_model = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "classifier",
                LogisticRegression(
                    solver="liblinear",
                    max_iter=1000,
                    random_state=random_state,
                ),
            ),
        ]
    )
    primary_model = HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_depth=3,
        max_iter=100,
        min_samples_leaf=2,
        random_state=random_state,
    )

    baseline_model.fit(X_train, y_train)
    primary_model.fit(X_train, y_train)
    baseline_validation_scores = baseline_model.predict_proba(X_validation)[:, 1].astype(float).tolist()
    primary_validation_scores = primary_model.predict_proba(X_validation)[:, 1].astype(float).tolist()

    feature_schema_artifact = build_feature_schema_artifact(
        feature_schema_version=feature_schema_version,
        executed_profitability_path=executed_profitability_path,
        created_at=created_at_value,
    )

    model_payload = {
        "artifact_version": MODEL_ARTIFACT_VERSION,
        "model_version": model_version,
        "trainer_version": TRAINER_VERSION,
        "feature_schema_version": feature_schema_version,
        "label_contract_version": META_TRAINING_LABEL_CONTRACT_VERSION,
        "split_policy_version": str(manifest.get("split_policy_version") or META_TRAINING_SPLIT_POLICY_VERSION),
        "split_policy_hash": str(manifest.get("split_policy_hash") or ""),
        "random_state": int(random_state),
        "baseline_model_type": "LogisticRegression",
        "primary_model_type": "HistGradientBoostingClassifier",
        "feature_names": list(FEATURE_NAMES_IN_ORDER),
        "feature_defaults": dict(OPTIONAL_FEATURE_DEFAULTS),
        "train_row_count": len(split_rows["train"]),
        "validation_row_count": len(split_rows["validation"]),
        "test_row_count": len(split_rows["test"]),
        "baseline_model": baseline_model,
        "primary_model": primary_model,
        "created_at": created_at_value,
    }

    run_metadata = {
        "artifact_version": RUN_METADATA_ARTIFACT_VERSION,
        "run_id": run_id_value,
        "model_version": model_version,
        "trainer_version": TRAINER_VERSION,
        "feature_schema_version": feature_schema_version,
        "label_contract_version": META_TRAINING_LABEL_CONTRACT_VERSION,
        "split_policy_version": str(manifest.get("split_policy_version") or META_TRAINING_SPLIT_POLICY_VERSION),
        "split_policy_hash": str(manifest.get("split_policy_hash") or ""),
        "executed_profitability_path": executed_profitability_path,
        "split_manifest_path": split_manifest_path,
        "random_state": int(random_state),
        "baseline_model_type": "LogisticRegression",
        "primary_model_type": "HistGradientBoostingClassifier",
        "train_row_count": len(split_rows["train"]),
        "validation_row_count": len(split_rows["validation"]),
        "test_row_count": len(split_rows["test"]),
        "train_cluster_count": int(manifest.get("train_cluster_count") or 0),
        "validation_cluster_count": int(manifest.get("validation_cluster_count") or 0),
        "test_cluster_count": int(manifest.get("test_cluster_count") or 0),
        "train_candidate_ids": [str(row["candidate_id"]) for row in split_rows["train"]],
        "validation_candidate_ids": [str(row["candidate_id"]) for row in split_rows["validation"]],
        "test_candidate_ids": [str(row["candidate_id"]) for row in split_rows["test"]],
        "train_time_start": str(manifest.get("train_time_start") or ""),
        "train_time_end": str(manifest.get("train_time_end") or ""),
        "validation_time_start": str(manifest.get("validation_time_start") or ""),
        "validation_time_end": str(manifest.get("validation_time_end") or ""),
        "test_time_start": str(manifest.get("test_time_start") or ""),
        "test_time_end": str(manifest.get("test_time_end") or ""),
        "primary_model_frozen_before_validation_scoring": True,
        "validation_scoring": {
            "candidate_ids": [str(row["candidate_id"]) for row in split_rows["validation"]],
            "labels": [int(row["profitability_label"]) for row in split_rows["validation"]],
            "baseline_scores": baseline_validation_scores,
            "primary_scores": primary_validation_scores,
        },
        "feature_names": list(FEATURE_NAMES_IN_ORDER),
        "feature_schema_hash": feature_schema_artifact["schema_hash"],
        "created_at": created_at_value,
    }

    return {
        "model_payload": model_payload,
        "feature_schema": feature_schema_artifact,
        "run_metadata": run_metadata,
    }


def write_training_artifacts(
    *,
    executed_profitability_path: str,
    split_manifest_path: str,
    output_dir: str,
    run_id: Optional[str] = None,
    created_at: Optional[str] = None,
    random_state: int = DEFAULT_RANDOM_STATE,
) -> Dict[str, Any]:
    validated_inputs = load_validated_split_inputs(
        executed_profitability_path=executed_profitability_path,
        split_manifest_path=split_manifest_path,
    )
    trained = train_models_from_validated_inputs(
        validated_inputs,
        run_id=run_id,
        created_at=created_at,
        random_state=random_state,
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    model_path = output_path / MODEL_FILENAME
    feature_schema_path = output_path / FEATURE_SCHEMA_FILENAME
    run_metadata_path = output_path / RUN_METADATA_FILENAME

    joblib.dump(trained["model_payload"], model_path)
    with open(feature_schema_path, "w", encoding="utf-8") as feature_schema_file:
        json.dump(trained["feature_schema"], feature_schema_file, indent=2, sort_keys=True)
        feature_schema_file.write("\n")
    with open(run_metadata_path, "w", encoding="utf-8") as run_metadata_file:
        json.dump(trained["run_metadata"], run_metadata_file, indent=2, sort_keys=True)
        run_metadata_file.write("\n")

    return {
        "model_path": str(model_path),
        "feature_schema_path": str(feature_schema_path),
        "run_metadata_path": str(run_metadata_path),
        "model_payload": trained["model_payload"],
        "feature_schema": trained["feature_schema"],
        "run_metadata": trained["run_metadata"],
    }