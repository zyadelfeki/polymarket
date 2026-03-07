from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


TRAINING_REPORT_ARTIFACT_VERSION = 1
PROMOTABLE_MODEL_BUNDLE_ARTIFACT_VERSION = 1
PROMOTION_PIPELINE_VERSION = "meta_promotion_v1"
PROMOTION_GATE_VERSION = "meta_promotion_gate_v1"
STAGED_INPUT_CONTRACT_VERSION = "pre_calibration_staged_v1"
PROMOTABLE_CONTRACT_VERSION = "promotable_offline_v1"

TRAINING_REPORT_FILENAME = "training_report.json"
PROMOTABLE_MODEL_BUNDLE_FILENAME = "promotable_model_bundle.joblib"

VALIDATION_ONLY_FIT_SPLIT = "validation_only"
VALIDATION_ONLY_THRESHOLD_SELECTION_FLAG = "selected_before_test_evaluation"

TRAINING_REPORT_REQUIRED_FIELDS = (
    "artifact_version",
    "contract_version",
    "pipeline_version",
    "staged_input_contract",
    "staged_artifacts",
    "integrity",
    "calibration",
    "threshold_selection",
    "promotion_gate",
    "outputs",
)

PROMOTABLE_BUNDLE_REQUIRED_FIELDS = (
    "artifact_version",
    "contract_version",
    "pipeline_version",
    "model_version",
    "feature_schema_version",
    "label_contract_version",
    "split_policy_version",
    "split_policy_hash",
    "primary_model",
    "calibrator",
    "selected_threshold",
    "feature_names",
    "feature_defaults",
    "staged_feature_schema_path",
    "training_report_path",
)

FEATURE_SCHEMA_REQUIRED_FIELDS = (
    "artifact_version",
    "feature_schema_version",
    "feature_names_in_order",
    "schema_hash",
    "required_features",
    "optional_features_with_defaults",
    "forbidden_features",
)


def build_staged_input_contract() -> dict[str, str]:
    return {
        "version": STAGED_INPUT_CONTRACT_VERSION,
        "description": "Ticket 4.2 staged pre-calibration artifacts consumed as immutable inputs.",
        "extends_to": PROMOTABLE_CONTRACT_VERSION,
    }


def find_missing_fields(payload: Mapping[str, Any], required_fields: Sequence[str]) -> list[str]:
    return sorted(field for field in required_fields if payload.get(field) is None)


def ensure_required_fields(
    payload: Mapping[str, Any],
    required_fields: Sequence[str],
    *,
    artifact_name: str,
) -> None:
    missing = find_missing_fields(payload, required_fields)
    if missing:
        raise ValueError(f"{artifact_name} missing required fields: {', '.join(missing)}")


def find_expected_string_mismatch(
    payload: Mapping[str, Any],
    field_name: str,
    expected_value: str,
) -> str | None:
    actual_value = str(payload.get(field_name) or "")
    if actual_value != str(expected_value):
        return actual_value
    return None


def normalize_path_for_compare(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return str(Path(raw).resolve(strict=False)).lower()