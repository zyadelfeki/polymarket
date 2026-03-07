"""
Meta-Labeling Gate for Charlie trade signals.

Architecture
-----------
A lightweight binary classifier (Logistic Regression by default, LightGBM when
lgbm_available and >=200 labelled rows) trained on historical settled trades.

Label:  1 = final PnL > 0 after settlement   (TAKE the trade)
        0 = final PnL <= 0                    (SKIP the trade)

Features extracted from ``order_tracking`` + log events:
  charlie_p_win_raw   – raw (uncalibrated) Charlie p_win
  net_edge            – fee-adjusted edge at signal time
  fee                 – taker fee rate applied
  implied_prob        – market price at signal time
  confidence          – Charlie ensemble confidence
  ofi_conflict        – 1 if OFI conflicted with Charlie, else 0
  hour_sin / hour_cos – time-of-day encoding (circular)
  dow_sin / dow_cos   – day-of-week encoding  (circular)
  rolling_win_rate    – recent win-rate over last 20 settled trades
  rolling_pnl_z       – z-scored rolling PnL (last 10 trades)

Training
--------
Run offline:
    python -m ml.meta_gate --train [--db data/trading.db]

This writes ``models/meta_gate.pkl`` and emits AUC, precision/recall,
and calibration curve stats to stdout.

Inference
---------
from ml.meta_gate import should_trade
take_it, proba = should_trade(features)

Fail-open contract: if the model file is absent or corrupt, ``should_trade``
returns True and logs a WARNING.  Never raises.

Latency budget: <2 ms per call (single logistic regression prediction).

IRONCLAD:
- No I/O on the hot path.  Model is loaded once at import time.
- No multiprocessing or threading inside inference.
- Threadsafety: inference path writes to CPython int counters (_meta_gate_*).
  Safe under CPython GIL.  For non-CPython runtimes, wrap counter updates
  in a threading.Lock() if needed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import joblib
import structlog
import math
import os
import pickle
import sqlite3
import sys
import threading
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ml import meta_promoted_contract

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODEL_PATH = _REPO_ROOT / "models" / "meta_gate.pkl"
_PROMOTABLE_MODEL_BUNDLE_PATH = _REPO_ROOT / "models" / "meta_gate" / "staging" / "final" / "promotable_model_bundle.joblib"
_PROMOTION_REPORT_PATH = _REPO_ROOT / "models" / "meta_gate" / "staging" / "final" / "training_report.json"
_DB_PATH    = _REPO_ROOT / "data" / "trading.db"
# JSON file used to persist runtime threshold changes across restarts.
# Takes priority over config_production.META_GATE_THRESHOLD when present.
_THRESHOLD_OVERRIDE_PATH = _REPO_ROOT / "models" / "meta_gate_threshold.json"

_PROMOTABLE_CONTRACT_VERSION = meta_promoted_contract.PROMOTABLE_CONTRACT_VERSION
_PROMOTION_GATE_VERSION = meta_promoted_contract.PROMOTION_GATE_VERSION
_PROMOTION_PIPELINE_VERSION = meta_promoted_contract.PROMOTION_PIPELINE_VERSION

SHADOW_SCORING_MODE_VALID_BUNDLE = "valid_promoted_bundle"
SHADOW_SCORING_MODE_SCHEMA_MISMATCH = "schema_mismatch_observational"
SHADOW_SCORING_MODE_FALLBACK = "fallback_observational"

SHADOW_ARTIFACT_LOAD_STATUS_LOADED = "loaded"
SHADOW_ARTIFACT_LOAD_STATUS_FAILED = "load_failed"

SHADOW_SESSION_SUMMARY_ARTIFACT_VERSION = 1
SHADOW_SESSION_SUMMARY_SCHEMA_VERSION = "shadow_session_summary_v1"
SHADOW_REPLAY_AGREEMENT_ARTIFACT_VERSION = 1
SHADOW_REPLAY_AGREEMENT_SCHEMA_VERSION = "shadow_replay_agreement_v1"
SHADOW_REPLAY_P_PROFIT_TOLERANCE = 1e-9

SHADOW_SCORING_EVENT_FIELDS = (
    "artifact_load_status",
    "artifact_contract_version",
    "model_version",
    "feature_schema_version",
    "calibration_version",
    "scoring_mode",
    "fallback_reason",
    "selected_threshold",
    "p_profit",
    "allow_trade",
    "effective_allow_trade",
    "shadow_only",
    "decision_mode",
    "integrity_flags",
    "expected_return_bps",
    "size_multiplier",
    "block_reason",
    "training_eligibility",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_shadow_session_id() -> str:
    return f"meta_shadow_session_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

# ---------------------------------------------------------------------------
# Threshold: meta-gate rejects when P(take) < threshold.
# Priority order:
#   1. models/meta_gate_threshold.json  (written by set_threshold() / trainer)
#   2. config_production.META_GATE_THRESHOLD
#   3. Hard-coded default 0.50
# ---------------------------------------------------------------------------
def _load_threshold() -> float:
    """Read threshold, preferring the persisted JSON override."""
    # 1. Check persisted override from set_threshold() or trainer
    try:
        if _THRESHOLD_OVERRIDE_PATH.exists():
            import json as _json
            data = _json.loads(_THRESHOLD_OVERRIDE_PATH.read_text())
            val = float(data.get("threshold", 0.50))
            logger.info("meta_gate_threshold_loaded_from_file", path=str(_THRESHOLD_OVERRIDE_PATH), threshold=val)
            return val
    except Exception:
        pass
    # 2. Fall back to config_production constant
    try:
        import importlib
        cfg = importlib.import_module("config_production")
        return float(getattr(cfg, "META_GATE_THRESHOLD", 0.50))
    except Exception:
        return 0.50

_DEFAULT_THRESHOLD: float = _load_threshold()


def reload_model() -> None:
    """
    Reset the model cache so the next ``should_trade()`` call re-reads the
    model from disk.  Use this after retraining (``python -m ml.meta_gate
    --train``) without restarting the bot process.

    Thread-safe: acquires ``_model_load_lock`` before mutating cache globals.
    """
    global _MODEL_CACHE, _MODEL_LOAD_ATTEMPTED
    with _model_load_lock:
        _MODEL_CACHE = _NOT_LOADED
        _MODEL_LOAD_ATTEMPTED = False
    logger.info("meta_gate_model_cache_reset")


def set_threshold(value: float) -> None:
    """
    Update the global default classification threshold at runtime and persist
    it to ``models/meta_gate_threshold.json`` so the new value survives a
    restart without requiring a code change.

    Useful for tightening the gate in a bad regime without restarting the bot.
    The new threshold only applies to trades evaluated *after* this call;
    ongoing inference in-flight is not affected.

    Parameters
    ----------
    value:
        New threshold in [0.0, 1.0].  Values outside the range are clamped.
    """
    import json as _json
    from datetime import datetime, timezone
    global _DEFAULT_THRESHOLD
    clamped = max(0.0, min(1.0, float(value)))
    _DEFAULT_THRESHOLD = clamped
    # Persist so the value survives a restart.
    try:
        _THRESHOLD_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "threshold": clamped,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        _THRESHOLD_OVERRIDE_PATH.write_text(_json.dumps(payload, indent=2))
        logger.info("meta_gate_threshold_persisted", path=str(_THRESHOLD_OVERRIDE_PATH), new_threshold=clamped)
    except Exception as exc:
        logger.warning("meta_gate_threshold_persist_failed", error=str(exc))
    logger.info("meta_gate_threshold_updated", new_threshold=clamped)


# ---------------------------------------------------------------------------
# Lazy model cache — loaded once on first call to should_trade().
# Thread-safe: double-checked locking guards the disk-read on first access.
#
# _MODEL_CACHE states:
#   _NOT_LOADED  → load not yet attempted (initial value).
#   False        → load attempted but failed (fail-open mode).
#   dict object  → loaded successfully.
# ---------------------------------------------------------------------------
_NOT_LOADED: object = object()       # Sentinel: distinguishes "never tried" from None
_MODEL_CACHE: object = _NOT_LOADED   # Set by _get_model()
_MODEL_LOAD_ATTEMPTED: bool = False  # Guard against repeated load attempts
_model_load_lock = threading.Lock()  # Ensures exactly-once load under concurrency

# ---------------------------------------------------------------------------
# Session-level counters for get_session_meta_gate_stats()
# ---------------------------------------------------------------------------
_meta_gate_approved: int = 0
_meta_gate_rejected: int = 0
_meta_gate_errors: int = 0
_meta_gate_shadow_decisions: int = 0
_meta_gate_shadow_rejections: int = 0
_meta_gate_shadow_fallbacks: int = 0
_meta_gate_shadow_feature_mismatches: int = 0
_meta_gate_shadow_load_successes: int = 0
_meta_gate_shadow_load_failures: int = 0
_meta_gate_shadow_load_failure_reasons: Dict[str, int] = {}
_meta_gate_shadow_last_load_failure_reason: Optional[str] = None
_meta_gate_shadow_valid_promoted_bundle_decisions: int = 0
_meta_gate_shadow_schema_mismatch_decisions: int = 0
_meta_gate_shadow_scored_opportunities: int = 0
_meta_gate_shadow_unscored_opportunities: int = 0
_meta_gate_shadow_fallback_decisions_by_reason: Dict[str, int] = {}
_meta_gate_shadow_artifact_load_status_counts: Dict[str, int] = {}
_meta_gate_shadow_model_versions_observed: Dict[str, int] = {}
_meta_gate_shadow_feature_schema_versions_observed: Dict[str, int] = {}
_meta_gate_shadow_calibration_versions_observed: Dict[str, int] = {}
_meta_gate_shadow_selected_threshold_counts: Dict[str, int] = {}
_meta_gate_shadow_last_selected_threshold: Optional[float] = None
_meta_gate_shadow_integrity_flags_observed: Dict[str, int] = {}
_meta_gate_shadow_block_reasons_observed: Dict[str, int] = {}
_meta_gate_shadow_decision_mode_counts: Dict[str, int] = {}
_meta_gate_shadow_effective_allow_trade_true: int = 0
_meta_gate_shadow_effective_allow_trade_false: int = 0
_meta_gate_shadow_shadow_only_true: int = 0
_meta_gate_shadow_shadow_only_false: int = 0
_meta_gate_shadow_p_profit_sum: float = 0.0
_meta_gate_shadow_p_profit_count: int = 0
_meta_gate_shadow_p_profit_min: Optional[float] = None
_meta_gate_shadow_p_profit_max: Optional[float] = None
_meta_gate_shadow_session_id: str = _new_shadow_session_id()
_meta_gate_shadow_session_started_at: str = _utc_now_iso()

_SHADOW_MODEL_CACHE: object = _NOT_LOADED
_SHADOW_MODEL_LOAD_ATTEMPTED: bool = False
_shadow_model_load_lock = threading.Lock()


class ShadowArtifactValidationError(ValueError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = str(reason_code)


def reload_shadow_runtime_bundle() -> None:
    global _SHADOW_MODEL_CACHE, _SHADOW_MODEL_LOAD_ATTEMPTED
    with _shadow_model_load_lock:
        _SHADOW_MODEL_CACHE = _NOT_LOADED
        _SHADOW_MODEL_LOAD_ATTEMPTED = False
    logger.info("meta_gate_shadow_artifact_cache_reset")


@dataclass
class MetaRuntimeDecision:
    """Shadow-mode meta-gate verdict.

    `allow_trade` is the model's hypothetical decision, not the runtime's
    effective permission. Ticket 3 keeps runtime behavior observational only;
    callers must use `shadow_only` to avoid wiring this verdict into execution.
    """
    allow_trade: bool
    p_profit: float
    raw_p_profit: Optional[float]
    expected_return_bps: float
    size_multiplier: float
    block_reason: Optional[str]
    fallback_reason: Optional[str]
    artifact_load_status: str
    artifact_contract_version: str
    model_version: str
    feature_schema_version: str
    calibration_version: str
    selected_threshold: Optional[float]
    scoring_mode: str
    effective_allow_trade: bool
    decision_mode: str
    shadow_only: bool
    training_eligibility: str
    integrity_flags: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "artifact_load_status": str(self.artifact_load_status),
            "artifact_contract_version": str(self.artifact_contract_version),
            "model_version": str(self.model_version),
            "feature_schema_version": str(self.feature_schema_version),
            "calibration_version": str(self.calibration_version),
            "scoring_mode": str(self.scoring_mode),
            "fallback_reason": self.fallback_reason,
            "selected_threshold": (
                None if self.selected_threshold is None else float(self.selected_threshold)
            ),
            "p_profit": float(self.p_profit),
            "allow_trade": bool(self.allow_trade),
            "effective_allow_trade": bool(self.effective_allow_trade),
            "shadow_only": bool(self.shadow_only),
            "decision_mode": str(self.decision_mode),
            "integrity_flags": list(self.integrity_flags),
            "expected_return_bps": float(self.expected_return_bps),
            "size_multiplier": float(self.size_multiplier),
            "block_reason": self.block_reason,
            "training_eligibility": str(self.training_eligibility),
        }


def _increment_counter_bucket(bucket: Dict[str, int], key: Optional[str]) -> None:
    normalized_key = str(key or "unknown")
    bucket[normalized_key] = bucket.get(normalized_key, 0) + 1


def _record_shadow_session_observability(decision: MetaRuntimeDecision) -> None:
    global _meta_gate_shadow_last_selected_threshold
    global _meta_gate_shadow_effective_allow_trade_true
    global _meta_gate_shadow_effective_allow_trade_false
    global _meta_gate_shadow_shadow_only_true
    global _meta_gate_shadow_shadow_only_false
    global _meta_gate_shadow_p_profit_sum
    global _meta_gate_shadow_p_profit_count
    global _meta_gate_shadow_p_profit_min
    global _meta_gate_shadow_p_profit_max

    _increment_counter_bucket(_meta_gate_shadow_artifact_load_status_counts, decision.artifact_load_status)
    _increment_counter_bucket(_meta_gate_shadow_model_versions_observed, decision.model_version)
    _increment_counter_bucket(_meta_gate_shadow_feature_schema_versions_observed, decision.feature_schema_version)
    _increment_counter_bucket(_meta_gate_shadow_calibration_versions_observed, decision.calibration_version)
    _increment_counter_bucket(_meta_gate_shadow_decision_mode_counts, decision.decision_mode)

    if decision.selected_threshold is not None:
        threshold_key = format(float(decision.selected_threshold), ".12g")
        _increment_counter_bucket(_meta_gate_shadow_selected_threshold_counts, threshold_key)
        _meta_gate_shadow_last_selected_threshold = float(decision.selected_threshold)

    if decision.block_reason:
        _increment_counter_bucket(_meta_gate_shadow_block_reasons_observed, decision.block_reason)

    for integrity_flag in decision.integrity_flags:
        _increment_counter_bucket(_meta_gate_shadow_integrity_flags_observed, integrity_flag)

    if decision.effective_allow_trade:
        _meta_gate_shadow_effective_allow_trade_true += 1
    else:
        _meta_gate_shadow_effective_allow_trade_false += 1

    if decision.shadow_only:
        _meta_gate_shadow_shadow_only_true += 1
    else:
        _meta_gate_shadow_shadow_only_false += 1

    if decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE:
        _meta_gate_shadow_p_profit_sum += float(decision.p_profit)
        _meta_gate_shadow_p_profit_count += 1
        if _meta_gate_shadow_p_profit_min is None or float(decision.p_profit) < _meta_gate_shadow_p_profit_min:
            _meta_gate_shadow_p_profit_min = float(decision.p_profit)
        if _meta_gate_shadow_p_profit_max is None or float(decision.p_profit) > _meta_gate_shadow_p_profit_max:
            _meta_gate_shadow_p_profit_max = float(decision.p_profit)


def _build_p_profit_summary() -> Dict[str, Optional[float]]:
    mean_value = None
    if _meta_gate_shadow_p_profit_count > 0:
        mean_value = _meta_gate_shadow_p_profit_sum / _meta_gate_shadow_p_profit_count
    return {
        "count": _meta_gate_shadow_p_profit_count,
        "min": _meta_gate_shadow_p_profit_min,
        "max": _meta_gate_shadow_p_profit_max,
        "mean": mean_value,
    }


def _write_json_artifact(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def build_shadow_session_summary(
    *,
    session_id: Optional[str] = None,
    exported_at: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "artifact_version": SHADOW_SESSION_SUMMARY_ARTIFACT_VERSION,
        "schema_version": SHADOW_SESSION_SUMMARY_SCHEMA_VERSION,
        "session_id": str(session_id or _meta_gate_shadow_session_id),
        "started_at": str(_meta_gate_shadow_session_started_at),
        "exported_at": str(exported_at or _utc_now_iso()),
        "artifact_load_status_summary": dict(_meta_gate_shadow_artifact_load_status_counts),
        "decision_counts": {
            "shadow_decisions": _meta_gate_shadow_decisions,
            "valid_promoted_bundle_decisions": _meta_gate_shadow_valid_promoted_bundle_decisions,
            "shadow_rejections": _meta_gate_shadow_rejections,
            "fallback_decisions": _meta_gate_shadow_fallbacks,
            "fallback_decisions_by_reason": dict(_meta_gate_shadow_fallback_decisions_by_reason),
            "schema_mismatch_decisions": _meta_gate_shadow_schema_mismatch_decisions,
            "scored_opportunities": _meta_gate_shadow_scored_opportunities,
            "unscored_opportunities": _meta_gate_shadow_unscored_opportunities,
        },
        "p_profit_summary": _build_p_profit_summary(),
        "threshold_summary": {
            "selected_threshold_counts": dict(_meta_gate_shadow_selected_threshold_counts),
            "last_selected_threshold": _meta_gate_shadow_last_selected_threshold,
        },
        "observed_versions": {
            "model_version_counts": dict(_meta_gate_shadow_model_versions_observed),
            "feature_schema_version_counts": dict(_meta_gate_shadow_feature_schema_versions_observed),
            "calibration_version_counts": dict(_meta_gate_shadow_calibration_versions_observed),
        },
        "integrity_flags_observed": dict(_meta_gate_shadow_integrity_flags_observed),
        "block_reasons_observed": dict(_meta_gate_shadow_block_reasons_observed),
        "observational_contract": {
            "decision_mode_counts": dict(_meta_gate_shadow_decision_mode_counts),
            "effective_allow_trade_true_count": _meta_gate_shadow_effective_allow_trade_true,
            "effective_allow_trade_false_count": _meta_gate_shadow_effective_allow_trade_false,
            "shadow_only_true_count": _meta_gate_shadow_shadow_only_true,
            "shadow_only_false_count": _meta_gate_shadow_shadow_only_false,
        },
    }


def export_shadow_session_summary(
    output_path: str | Path,
    *,
    session_id: Optional[str] = None,
    exported_at: Optional[str] = None,
) -> Dict[str, object]:
    summary = build_shadow_session_summary(session_id=session_id, exported_at=exported_at)
    resolved_path = Path(output_path)
    _write_json_artifact(resolved_path, summary)
    logger.info(
        "meta_gate_shadow_session_summary_exported",
        path=str(resolved_path),
        session_id=summary["session_id"],
        shadow_decisions=summary["decision_counts"]["shadow_decisions"],
    )
    return summary


def _build_model_unavailable_shadow_decision(
    *,
    expected_return_bps: float,
    expected_feature_schema_version: str,
    calibration_version: str,
    decision_mode: str,
    shadow_only: bool,
    integrity_flags: List[str],
    load_failure_reason: str,
) -> MetaRuntimeDecision:
    return MetaRuntimeDecision(
        allow_trade=True,
        p_profit=0.5,
        raw_p_profit=None,
        expected_return_bps=expected_return_bps,
        size_multiplier=1.0,
        block_reason="fallback_model_unavailable",
        fallback_reason=load_failure_reason,
        artifact_load_status=SHADOW_ARTIFACT_LOAD_STATUS_FAILED,
        artifact_contract_version="unavailable",
        model_version="fallback:no_model",
        feature_schema_version=str(expected_feature_schema_version),
        calibration_version=str(calibration_version),
        selected_threshold=None,
        scoring_mode=SHADOW_SCORING_MODE_FALLBACK,
        effective_allow_trade=True,
        decision_mode=decision_mode,
        shadow_only=shadow_only,
        training_eligibility="shadow_pending_outcome",
        integrity_flags=integrity_flags + ["fallback_decision", "model_unavailable", f"artifact_load_failure:{load_failure_reason}"],
    )


def _build_runtime_decision(
    features: dict,
    *,
    expected_feature_schema_version: str,
    calibration_version: str,
    decision_mode: str,
    shadow_only: bool,
) -> MetaRuntimeDecision:
    normalized_mode, normalized_shadow_only, expected_return_bps, integrity_flags = _normalize_shadow_context(
        features,
        decision_mode=decision_mode,
        shadow_only=shadow_only,
    )

    model_bundle = _get_shadow_runtime_model()
    if model_bundle is None:
        load_failure_reason = _meta_gate_shadow_last_load_failure_reason or "model_unavailable"
        return _build_model_unavailable_shadow_decision(
            expected_return_bps=expected_return_bps,
            expected_feature_schema_version=expected_feature_schema_version,
            calibration_version=calibration_version,
            decision_mode=normalized_mode,
            shadow_only=normalized_shadow_only,
            integrity_flags=integrity_flags,
            load_failure_reason=load_failure_reason,
        )

    return _score_promoted_shadow_bundle(
        model_bundle,
        features,
        expected_feature_schema_version=expected_feature_schema_version,
        calibration_version=calibration_version,
        decision_mode=normalized_mode,
        shadow_only=normalized_shadow_only,
    )


def build_shadow_replay_agreement_report(
    feature_batches: List[dict],
    *,
    expected_feature_schema_version: str = "meta_candidate_v1",
    calibration_version: str = "platt_scaler_v1",
    decision_mode: str = "shadow",
    shadow_only: bool = True,
    tolerance: float = SHADOW_REPLAY_P_PROFIT_TOLERANCE,
    mismatch_sample_limit: int = 5,
    report_id: Optional[str] = None,
    exported_at: Optional[str] = None,
) -> Dict[str, object]:
    model_bundle = _get_shadow_runtime_model()
    mismatch_examples: List[Dict[str, object]] = []
    disagreement_examples_count = 0
    input_count = 0
    valid_scored_count = 0
    fallback_count = 0
    schema_mismatch_count = 0
    artifact_load_status_summary: Dict[str, int] = {}
    fallback_counts_by_reason: Dict[str, int] = {}
    decision_mode_counts: Dict[str, int] = {}
    effective_allow_trade_true_count = 0
    effective_allow_trade_false_count = 0
    shadow_only_true_count = 0
    shadow_only_false_count = 0
    exact_match_count = 0
    tolerance_match_count = 0
    threshold_interpretation_agreement_count = 0
    compared_p_profit_count = 0

    for index, feature_row in enumerate(feature_batches, start=1):
        if not isinstance(feature_row, dict):
            raise TypeError(f"feature_batches[{index - 1}] must be a dict")

        input_count += 1
        runtime_decision = _build_runtime_decision(
            feature_row,
            expected_feature_schema_version=expected_feature_schema_version,
            calibration_version=calibration_version,
            decision_mode=decision_mode,
            shadow_only=shadow_only,
        )
        replay_decision = runtime_decision
        if model_bundle is not None:
            replay_decision = _score_promoted_shadow_bundle(
                model_bundle,
                feature_row,
                expected_feature_schema_version=expected_feature_schema_version,
                calibration_version=calibration_version,
                decision_mode=decision_mode,
                shadow_only=shadow_only,
            )

        _increment_counter_bucket(artifact_load_status_summary, runtime_decision.artifact_load_status)
        _increment_counter_bucket(decision_mode_counts, runtime_decision.decision_mode)

        if runtime_decision.effective_allow_trade:
            effective_allow_trade_true_count += 1
        else:
            effective_allow_trade_false_count += 1

        if runtime_decision.shadow_only:
            shadow_only_true_count += 1
        else:
            shadow_only_false_count += 1

        if runtime_decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE:
            valid_scored_count += 1
        elif runtime_decision.scoring_mode == SHADOW_SCORING_MODE_SCHEMA_MISMATCH:
            schema_mismatch_count += 1
            _increment_counter_bucket(fallback_counts_by_reason, runtime_decision.fallback_reason)
        elif runtime_decision.scoring_mode == SHADOW_SCORING_MODE_FALLBACK:
            fallback_count += 1
            _increment_counter_bucket(fallback_counts_by_reason, runtime_decision.fallback_reason)

        exact_match = runtime_decision.p_profit == replay_decision.p_profit
        tolerance_match = False
        threshold_agreement = False
        if (
            runtime_decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE
            and replay_decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE
        ):
            compared_p_profit_count += 1
            tolerance_match = abs(float(runtime_decision.p_profit) - float(replay_decision.p_profit)) <= float(tolerance)
            threshold_agreement = runtime_decision.allow_trade is replay_decision.allow_trade
            if exact_match:
                exact_match_count += 1
            if tolerance_match:
                tolerance_match_count += 1
            if threshold_agreement:
                threshold_interpretation_agreement_count += 1

        decision_match = (
            runtime_decision.scoring_mode == replay_decision.scoring_mode
            and runtime_decision.fallback_reason == replay_decision.fallback_reason
            and runtime_decision.allow_trade is replay_decision.allow_trade
            and (
                runtime_decision.scoring_mode != SHADOW_SCORING_MODE_VALID_BUNDLE
                or tolerance_match
            )
        )

        if not decision_match:
            disagreement_examples_count += 1
            if len(mismatch_examples) < max(0, int(mismatch_sample_limit)):
                mismatch_examples.append(
                    {
                        "index": index,
                        "runtime": runtime_decision.as_dict(),
                        "replay": replay_decision.as_dict(),
                    }
                )

    exact_match_rate = None
    tolerance_match_rate = None
    threshold_agreement_rate = None
    if compared_p_profit_count > 0:
        exact_match_rate = exact_match_count / compared_p_profit_count
        tolerance_match_rate = tolerance_match_count / compared_p_profit_count
        threshold_agreement_rate = threshold_interpretation_agreement_count / compared_p_profit_count

    return {
        "artifact_version": SHADOW_REPLAY_AGREEMENT_ARTIFACT_VERSION,
        "schema_version": SHADOW_REPLAY_AGREEMENT_SCHEMA_VERSION,
        "report_id": str(report_id or f"meta_shadow_replay_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"),
        "exported_at": str(exported_at or _utc_now_iso()),
        "input_count": input_count,
        "valid_scored_count": valid_scored_count,
        "fallback_count": fallback_count,
        "schema_mismatch_count": schema_mismatch_count,
        "artifact_load_status_summary": artifact_load_status_summary,
        "fallback_counts_by_reason": fallback_counts_by_reason,
        "p_profit_match": {
            "compared_count": compared_p_profit_count,
            "exact_match_count": exact_match_count,
            "exact_match_rate": exact_match_rate,
            "tolerance": float(tolerance),
            "tolerance_match_count": tolerance_match_count,
            "tolerance_match_rate": tolerance_match_rate,
        },
        "threshold_interpretation": {
            "compared_count": compared_p_profit_count,
            "agreement_count": threshold_interpretation_agreement_count,
            "agreement_rate": threshold_agreement_rate,
        },
        "disagreement_examples_count": disagreement_examples_count,
        "mismatch_examples": mismatch_examples,
        "observational_contract": {
            "decision_mode_counts": decision_mode_counts,
            "effective_allow_trade_true_count": effective_allow_trade_true_count,
            "effective_allow_trade_false_count": effective_allow_trade_false_count,
            "shadow_only_true_count": shadow_only_true_count,
            "shadow_only_false_count": shadow_only_false_count,
        },
    }


def export_shadow_replay_agreement_report(
    feature_batches: List[dict],
    output_path: str | Path,
    *,
    expected_feature_schema_version: str = "meta_candidate_v1",
    calibration_version: str = "platt_scaler_v1",
    decision_mode: str = "shadow",
    shadow_only: bool = True,
    tolerance: float = SHADOW_REPLAY_P_PROFIT_TOLERANCE,
    mismatch_sample_limit: int = 5,
    report_id: Optional[str] = None,
    exported_at: Optional[str] = None,
) -> Dict[str, object]:
    report = build_shadow_replay_agreement_report(
        feature_batches,
        expected_feature_schema_version=expected_feature_schema_version,
        calibration_version=calibration_version,
        decision_mode=decision_mode,
        shadow_only=shadow_only,
        tolerance=tolerance,
        mismatch_sample_limit=mismatch_sample_limit,
        report_id=report_id,
        exported_at=exported_at,
    )
    resolved_path = Path(output_path)
    _write_json_artifact(resolved_path, report)
    logger.info(
        "meta_gate_shadow_replay_agreement_report_exported",
        path=str(resolved_path),
        report_id=report["report_id"],
        input_count=report["input_count"],
        disagreement_examples_count=report["disagreement_examples_count"],
    )
    return report


def _normalize_shadow_context(
    features: dict,
    *,
    decision_mode: str,
    shadow_only: bool,
) -> Tuple[str, bool, float, List[str]]:
    normalized_mode = str(decision_mode or "shadow").strip().lower() or "shadow"
    normalized_shadow_only = bool(shadow_only)
    expected_return_bps = round(_safe_float(features.get("net_edge"), 0.0) * 10000.0, 4)
    integrity_flags = ["shadow_only_no_trade_impact"]

    if normalized_mode != "shadow" or not normalized_shadow_only:
        integrity_flags.append("shadow_mode_forced")
        normalized_mode = "shadow"
        normalized_shadow_only = True

    return normalized_mode, normalized_shadow_only, expected_return_bps, integrity_flags


def _record_shadow_decision_metrics(decision: MetaRuntimeDecision) -> None:
    global _meta_gate_shadow_decisions
    global _meta_gate_shadow_rejections
    global _meta_gate_shadow_fallbacks
    global _meta_gate_shadow_feature_mismatches
    global _meta_gate_shadow_valid_promoted_bundle_decisions
    global _meta_gate_shadow_schema_mismatch_decisions
    global _meta_gate_shadow_scored_opportunities
    global _meta_gate_shadow_unscored_opportunities

    _record_shadow_session_observability(decision)
    _meta_gate_shadow_decisions += 1

    if decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE:
        _meta_gate_shadow_valid_promoted_bundle_decisions += 1
        _meta_gate_shadow_scored_opportunities += 1
        if not decision.allow_trade:
            _meta_gate_shadow_rejections += 1
        return

    _meta_gate_shadow_unscored_opportunities += 1

    if decision.scoring_mode == SHADOW_SCORING_MODE_SCHEMA_MISMATCH:
        _meta_gate_shadow_schema_mismatch_decisions += 1
        _meta_gate_shadow_feature_mismatches += 1

    if decision.scoring_mode in {SHADOW_SCORING_MODE_SCHEMA_MISMATCH, SHADOW_SCORING_MODE_FALLBACK}:
        _meta_gate_shadow_fallbacks += 1
        _increment_counter_bucket(_meta_gate_shadow_fallback_decisions_by_reason, decision.fallback_reason)


def _emit_shadow_scoring_event(
    decision: MetaRuntimeDecision,
    *,
    raw_p_profit: Optional[float] = None,
    error: Optional[str] = None,
    warning: bool = False,
) -> MetaRuntimeDecision:
    _record_shadow_decision_metrics(decision)

    event_payload = decision.as_dict()
    if raw_p_profit is not None:
        event_payload["raw_p_profit"] = round(float(raw_p_profit), 4)
    if error is not None:
        event_payload["error"] = str(error)

    log_method = logger.warning if warning else logger.info
    log_method("meta_gate_shadow_decision", **event_payload)
    return decision


def _score_promoted_shadow_bundle(
    model_bundle: dict,
    features: dict,
    *,
    expected_feature_schema_version: str,
    calibration_version: str,
    decision_mode: str = "shadow",
    shadow_only: bool = True,
) -> MetaRuntimeDecision:
    normalized_mode, normalized_shadow_only, expected_return_bps, integrity_flags = _normalize_shadow_context(
        features,
        decision_mode=decision_mode,
        shadow_only=shadow_only,
    )
    feature_names: List[str] = list(model_bundle.get("feature_names") or [])
    provided_keys = set(features.keys())
    expected_keys = set(feature_names)
    missing_features = sorted(name for name in feature_names if name not in provided_keys)
    unexpected_features = sorted(name for name in provided_keys if name not in expected_keys)
    model_version = _build_model_version(model_bundle)
    artifact_contract_version = str(model_bundle.get("contract_version") or _PROMOTABLE_CONTRACT_VERSION)
    threshold = float(model_bundle.get("threshold", _DEFAULT_THRESHOLD))

    if missing_features or unexpected_features:
        mismatch_flags = list(integrity_flags)
        mismatch_flags.append("feature_schema_mismatch")
        if missing_features:
            mismatch_flags.append(f"missing_features:{','.join(missing_features)}")
        if unexpected_features:
            mismatch_flags.append(f"unexpected_features:{','.join(unexpected_features)}")
        return MetaRuntimeDecision(
            allow_trade=True,
            p_profit=0.5,
            raw_p_profit=None,
            expected_return_bps=expected_return_bps,
            size_multiplier=1.0,
            block_reason="feature_schema_mismatch",
            fallback_reason="feature_schema_mismatch",
            artifact_load_status=SHADOW_ARTIFACT_LOAD_STATUS_LOADED,
            artifact_contract_version=artifact_contract_version,
            model_version=model_version,
            feature_schema_version=str(model_bundle.get("feature_schema_version") or expected_feature_schema_version),
            calibration_version=str(model_bundle.get("calibration_version") or calibration_version),
            selected_threshold=threshold,
            scoring_mode=SHADOW_SCORING_MODE_SCHEMA_MISMATCH,
            effective_allow_trade=True,
            decision_mode=normalized_mode,
            shadow_only=normalized_shadow_only,
            training_eligibility="blocked_feature_mismatch",
            integrity_flags=mismatch_flags,
        )

    try:
        model = model_bundle["model"]
        calibrator = model_bundle.get("calibrator")
        X = _dict_to_array(features, feature_names)
        raw_p_profit = float(model.predict_proba(X)[0, 1])
        if calibrator is not None:
            calibrated_input = np.array([[raw_p_profit]], dtype=np.float32)
            p_profit = float(calibrator.predict_proba(calibrated_input)[0, 1])
        else:
            p_profit = raw_p_profit
        allow_trade = p_profit >= threshold
        return MetaRuntimeDecision(
            allow_trade=allow_trade,
            p_profit=p_profit,
            raw_p_profit=raw_p_profit,
            expected_return_bps=expected_return_bps,
            size_multiplier=1.0,
            block_reason=None if allow_trade else "p_profit_below_threshold",
            fallback_reason=None,
            artifact_load_status=SHADOW_ARTIFACT_LOAD_STATUS_LOADED,
            artifact_contract_version=artifact_contract_version,
            model_version=model_version,
            feature_schema_version=str(model_bundle.get("feature_schema_version") or expected_feature_schema_version),
            calibration_version=str(model_bundle.get("calibration_version") or calibration_version),
            selected_threshold=threshold,
            scoring_mode=SHADOW_SCORING_MODE_VALID_BUNDLE,
            effective_allow_trade=True,
            decision_mode=normalized_mode,
            shadow_only=normalized_shadow_only,
            training_eligibility="shadow_pending_outcome",
            integrity_flags=(
                integrity_flags if allow_trade else integrity_flags + ["shadow_rejection_no_trade_impact"]
            ),
        )
    except Exception as exc:
        return MetaRuntimeDecision(
            allow_trade=True,
            p_profit=0.5,
            raw_p_profit=None,
            expected_return_bps=expected_return_bps,
            size_multiplier=1.0,
            block_reason="fallback_inference_error",
            fallback_reason=f"inference_error:{type(exc).__name__}",
            artifact_load_status=SHADOW_ARTIFACT_LOAD_STATUS_LOADED,
            artifact_contract_version=artifact_contract_version,
            model_version=model_version,
            feature_schema_version=str(model_bundle.get("feature_schema_version") or expected_feature_schema_version),
            calibration_version=str(model_bundle.get("calibration_version") or calibration_version),
            selected_threshold=threshold,
            scoring_mode=SHADOW_SCORING_MODE_FALLBACK,
            effective_allow_trade=True,
            decision_mode=normalized_mode,
            shadow_only=normalized_shadow_only,
            training_eligibility="shadow_pending_outcome",
            integrity_flags=integrity_flags + ["fallback_decision", f"inference_error:{type(exc).__name__}"],
        )


def evaluate_runtime_decision(
    features: dict,
    *,
    expected_feature_schema_version: str = "meta_candidate_v1",
    calibration_version: str = "platt_scaler_v1",
    decision_mode: str = "shadow",
    shadow_only: bool = True,
) -> MetaRuntimeDecision:
    """
    Return a typed runtime decision object for shadow-mode meta-gate evaluation.

    Ticket 3 contract:
    - Shadow-mode only. The returned object must never change trading behavior.
    - Fallbacks are explicit and auditable.
    - Feature mismatches are surfaced via integrity_flags instead of silently
      trusting zero-filled inference.
    """
    decision = _build_runtime_decision(
        features,
        expected_feature_schema_version=expected_feature_schema_version,
        calibration_version=calibration_version,
        decision_mode=decision_mode,
        shadow_only=shadow_only,
    )
    raw_p_profit = decision.raw_p_profit if decision.scoring_mode == SHADOW_SCORING_MODE_VALID_BUNDLE else None
    error_text = decision.fallback_reason if decision.block_reason == "fallback_inference_error" else None
    return _emit_shadow_scoring_event(
        decision,
        raw_p_profit=raw_p_profit,
        error=error_text,
        warning=decision.scoring_mode != SHADOW_SCORING_MODE_VALID_BUNDLE,
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def should_trade(features: dict) -> tuple[bool, float]:
    """
    Return (decision, proba) where decision is True if the meta-gate recommends
    taking the trade and proba is the model's probability estimate.

    Parameters
    ----------
    features:
        Dict with keys matching those produced by extract_features_from_opportunity().
        Unknown keys are ignored; missing keys are filled with 0.0.

    Fail-open:
        Returns (True, 1.0) if the model is unavailable, so trades are never
        silently blocked by an infrastructure failure.
    """
    global _meta_gate_approved, _meta_gate_rejected, _meta_gate_errors

    model_bundle = _get_model()
    if model_bundle is None:
        # Fail-open: model unavailable → let the trade proceed.
        _meta_gate_approved += 1
        logger.info("meta_gate_decision", decision="approved", reason="fail_open")
        return True, 1.0

    try:
        model = model_bundle["model"]
        scaler = model_bundle.get("scaler")
        feature_names: List[str] = model_bundle["feature_names"]
        threshold: float = float(model_bundle.get("threshold", _DEFAULT_THRESHOLD))

        X = _dict_to_array(features, feature_names)

        if scaler is not None:
            X = scaler.transform(X)

        # predict_proba returns [[p_skip, p_take]]
        proba = model.predict_proba(X)[0, 1]
        decision = float(proba) >= threshold
        if decision:
            _meta_gate_approved += 1
        else:
            _meta_gate_rejected += 1
        logger.info(
            "meta_gate_decision",
            decision="approved" if decision else "rejected",
            proba=round(float(proba), 4),
            threshold=round(threshold, 4),
        )
        return decision, float(proba)

    except Exception as exc:
        logger.warning(
            "meta_gate_inference_error",
            error=str(exc),
            error_type=type(exc).__name__,
            reason="fail_open",
        )
        _meta_gate_errors += 1
        logger.info("meta_gate_decision", decision="approved", reason="error_fail_open")
        return True, 1.0


def extract_features_from_opportunity(
    *,
    charlie_p_win_raw: float = 0.5,
    net_edge: float = 0.0,
    fee: float = 0.0,
    implied_prob: float = 0.5,
    confidence: float = 0.0,
    ofi_conflict: bool = False,
    now: Optional[datetime] = None,
    rolling_win_rate: Optional[float] = None,
    rolling_pnl_z: Optional[float] = None,
) -> dict:
    """
    Construct the feature dict expected by ``should_trade``.

    Designed to be called from the execution path (main.py) right after
    Charlie approves and OFI is resolved.  All parameters are optional with
    safe defaults so callers can pass only what they have.

    ``rolling_pnl_z`` should be computed from the DB's settled trade history
    before the current trade settles — specifically (DESC order from DB):

        pnl_window = last N settled PnLs from order_tracking (newest first)
        rolling_pnl_z = (pnl_window[0] - mean(pnl_window[1:])) / (std(pnl_window[1:]) + 1e-9)

    This matches the training feature exactly (peers = all-but-most-recent).
    Passing None (the default) substitutes 0.0, which is the neutral/unknown prior.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    hour = now.hour + now.minute / 60.0
    dow = now.weekday()  # 0=Mon … 6=Sun

    return {
        "charlie_p_win_raw":  charlie_p_win_raw,
        "net_edge":           net_edge,
        "fee":                fee,
        "implied_prob":       implied_prob,
        "confidence":         confidence,
        "ofi_conflict":       float(ofi_conflict),
        "hour_sin":           math.sin(2 * math.pi * hour / 24.0),
        "hour_cos":           math.cos(2 * math.pi * hour / 24.0),
        "dow_sin":            math.sin(2 * math.pi * dow / 7.0),
        "dow_cos":            math.cos(2 * math.pi * dow / 7.0),
        "rolling_win_rate":   rolling_win_rate if rolling_win_rate is not None else 0.5,
        "rolling_pnl_z":      rolling_pnl_z if rolling_pnl_z is not None else 0.0,
    }


# ---------------------------------------------------------------------------
# Training (run offline — never called from the hot path)
# ---------------------------------------------------------------------------


def build_training_dataset(db_path: str = str(_DB_PATH)) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """
    Extract features and labels from ``order_tracking`` (settled trades only).

    Returns (X, y, feature_names) where X.shape = (n_samples, n_features).
    Raises ValueError if < 20 labelled samples are available.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            order_id,
            market_id,
            opened_at,
            pnl,
            charlie_p_win,
            charlie_conf,
            notes
        FROM order_tracking
        WHERE order_state = 'SETTLED'
          AND pnl IS NOT NULL
        ORDER BY opened_at ASC
        """
    ).fetchall()
    conn.close()

    if len(rows) < 20:
        raise ValueError(
            f"Only {len(rows)} settled trades available; need >=20 to train the meta-gate. "
            "Run more paper-trading sessions to accumulate data."
        )

    FEATURE_NAMES = [
        "charlie_p_win_raw",
        "net_edge",
        "fee",
        "implied_prob",
        "confidence",
        "ofi_conflict",
        "hour_sin",
        "hour_cos",
        "dow_sin",
        "dow_cos",
        "rolling_win_rate",
        "rolling_pnl_z",
    ]

    X_list: List[List[float]] = []
    y_list: List[int] = []

    # Rolling stats window
    pnl_window: List[float] = []
    win_window: List[int] = []
    WINDOW_PNL = 10
    WINDOW_WIN = 20

    for row in rows:
        try:
            pnl = float(row["pnl"])
        except (TypeError, ValueError):
            continue

        label = int(pnl > 0)

        # Extract features from notes / columns (best-effort)
        notes = row["notes"] or ""
        charlie_p_win_raw = _safe_float(row["charlie_p_win"], 0.5)
        confidence        = _safe_float(row["charlie_conf"], 0.5)

        # Parse net_edge, fee, implied_prob from the reason string stored in notes
        net_edge    = _parse_notes_field(notes, "edge",    0.0)
        implied_prob = _parse_notes_field(notes, "implied", 0.5)
        fee          = _parse_notes_field(notes, "fee",     0.0)
        # net_edge stored in notes is already fee-adjusted; if absent derive from p_win
        if net_edge == 0.0 and charlie_p_win_raw != 0.5:
            net_edge = charlie_p_win_raw - implied_prob - fee

        # OFI conflict flag: notes contains "ofi_conflict" when halved
        ofi_conflict = float("ofi_conflict" in notes.lower() or "size_after_halving" in notes.lower())

        # Time-of-day
        try:
            ts = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)
        hour = ts.hour + ts.minute / 60.0
        dow  = ts.weekday()

        # Rolling stats
        # Use a fixed-window slice for the denominator so training matches
        # inference exactly.  Using len(win_window) here would cause the
        # denominator to grow unboundedly while the numerator sums at most
        # WINDOW_WIN entries — a systematic train/inference distribution mismatch.
        _recent = win_window[-WINDOW_WIN:]
        rolling_win_rate = sum(_recent) / len(_recent) if _recent else 0.5
        # Compute z-score using only PREVIOUS window (before appending current
        # pnl) so the current trade's PnL (== label) is never used as a feature.
        # arr[-1] is the most-recent PREVIOUS trade's PnL.
        if len(pnl_window) >= 2:
            arr = np.array(pnl_window[-WINDOW_PNL:])
            if len(arr) >= 2:
                peers = arr[:-1]   # exclude most recent trade from its own mean
                mu = float(peers.mean())
                sigma = float(peers.std()) + 1e-9
            else:
                mu, sigma = 0.0, 1.0
            rolling_pnl_z = (arr[-1] - mu) / sigma  # no self-contamination
        else:
            rolling_pnl_z = 0.0

        # Append AFTER feature computation — no look-ahead
        pnl_window.append(pnl)
        win_window.append(label)

        X_list.append([
            charlie_p_win_raw,
            net_edge,
            fee,
            implied_prob,
            confidence,
            ofi_conflict,
            math.sin(2 * math.pi * hour / 24.0),
            math.cos(2 * math.pi * hour / 24.0),
            math.sin(2 * math.pi * dow / 7.0),
            math.cos(2 * math.pi * dow / 7.0),
            rolling_win_rate,
            rolling_pnl_z,
        ])
        y_list.append(label)

    if len(X_list) < 20:
        raise ValueError(
            f"After feature extraction only {len(X_list)} clean samples remain; need >=20."
        )

    return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.int32), FEATURE_NAMES


def train_and_persist(
    db_path: str = str(_DB_PATH),
    model_path: str = str(_MODEL_PATH),
    threshold: float = _DEFAULT_THRESHOLD,
    use_lgbm: bool = False,
) -> Dict:
    """
    Train the meta-gate model and write it to disk.

    Returns a dict with AUC, precision, recall, and calibration ECE.
    Raises on data or model errors — never silently persists a broken model.
    """
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, precision_recall_curve
    from sklearn.model_selection import train_test_split
    from sklearn.preprocessing import StandardScaler
    from sklearn.calibration import CalibratedClassifierCV

    X, y, feature_names = build_training_dataset(db_path)
    n_samples = len(y)
    n_positive = int(y.sum())
    logger.info(
        "meta_gate_training_start",
        n_samples=n_samples,
        n_positive=n_positive,
        n_negative=n_samples - n_positive,
    )

    # Stratified split: 80% train, 20% validation (preserve class balance)
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=0.2, stratify=y, random_state=42
    )

    # Feature scaling (improves LR convergence; neutral for tree models)
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_val_scaled   = scaler.transform(X_val)

    if use_lgbm and n_samples >= 200:
        try:
            import lightgbm as lgb  # type: ignore
            base_model = lgb.LGBMClassifier(
                n_estimators=100,
                learning_rate=0.05,
                num_leaves=15,
                min_child_samples=10,
                class_weight="balanced",
                random_state=42,
                verbose=-1,
                n_jobs=1,
            )
            logger.info("meta_gate_using_lgbm")
        except ImportError:
            logger.warning("meta_gate_lgbm_unavailable_falling_back_to_lr")
            use_lgbm = False

    if not use_lgbm:
        base_model = LogisticRegression(
            class_weight="balanced",
            max_iter=500,
            random_state=42,
            C=1.0,
            solver="lbfgs",
        )
        logger.info("meta_gate_using_logistic_regression")

    # Isotonic calibration for probability estimates
    model = CalibratedClassifierCV(base_model, method="isotonic", cv=3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X_train_scaled, y_train)

    # --- Evaluation metrics ---
    proba_val  = model.predict_proba(X_val_scaled)[:, 1]
    pred_val   = (proba_val >= threshold).astype(int)

    # AUC
    if len(set(y_val)) < 2:
        auc = float("nan")
        logger.warning("meta_gate_val_only_one_class_in_y_val — AUC undefined")
    else:
        auc = float(roc_auc_score(y_val, proba_val))

    # Precision / recall at threshold
    tp = int(((pred_val == 1) & (y_val == 1)).sum())
    fp = int(((pred_val == 1) & (y_val == 0)).sum())
    fn = int(((pred_val == 0) & (y_val == 1)).sum())
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")

    # Expected Calibration Error (ECE, 10 bins)
    ece = _compute_ece(proba_val, y_val, n_bins=10)

    # Coverage: fraction of trades the gate would ALLOW
    coverage = float(pred_val.mean())

    metrics = {
        "n_train": len(X_train),
        "n_val":   len(X_val),
        "auc":      round(auc, 4),
        "precision": round(precision, 4),
        "recall":   round(recall, 4),
        "ece":      round(ece, 4),
        "coverage": round(coverage, 4),
        "threshold": threshold,
    }
    logger.info("meta_gate_training_complete", **metrics)

    # Warn on suspicious results (don't block persist — operator decides)
    if auc < 0.52 and not math.isnan(auc):
        logger.warning(
            "meta_gate_low_auc",
            auc=auc,
            msg="Model barely better than random — consider collecting more data before activating gate.",
        )
    if coverage < 0.2:
        logger.warning(
            "meta_gate_very_low_coverage",
            coverage=coverage,
            msg="Gate would block >80% of trades — review threshold.",
        )

    # Persist model bundle
    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "model":          model,
        "scaler":         scaler,
        "feature_names":  feature_names,
        "threshold":      threshold,
        "trained_at":     datetime.now(timezone.utc).isoformat(),
        "metrics":        metrics,
        "use_lgbm":       use_lgbm,
    }
    tmp_path = model_path + ".tmp"
    with open(tmp_path, "wb") as fh:
        pickle.dump(bundle, fh, protocol=5)
    os.replace(tmp_path, model_path)
    logger.info("meta_gate_model_persisted", path=model_path)

    print("\n=== Meta-Gate Training Results ===")
    for k, v in metrics.items():
        print(f"  {k:>12}: {v}")
    print(f"\n  Model saved to: {model_path}")

    return metrics


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_model() -> Optional[dict]:
    """
    Load model from disk (once) and return the bundle, or None on failure.

    Thread-safe via double-checked locking.  On the hot path (model already
    loaded) there is no lock acquisition — only a single bool read.
    """
    global _MODEL_CACHE, _MODEL_LOAD_ATTEMPTED

    # Fast path: load already attempted (bool read is effectively atomic in CPython).
    if _MODEL_LOAD_ATTEMPTED:
        # NOTE: _MODEL_CACHE can never be _NOT_LOADED here — _MODEL_LOAD_ATTEMPTED
        # is only set True after _MODEL_CACHE is assigned to False or a valid
        # bundle.  The `is _NOT_LOADED` check is kept for defensive correctness
        # only; it is unreachable in practice.
        return None if (_MODEL_CACHE is False or _MODEL_CACHE is _NOT_LOADED) else _MODEL_CACHE

    # Slow path: first call — acquire lock then re-check (double-checked locking).
    with _model_load_lock:
        if _MODEL_LOAD_ATTEMPTED:
            return None if (_MODEL_CACHE is False or _MODEL_CACHE is _NOT_LOADED) else _MODEL_CACHE

        # Mark attempted before any I/O so a failed load is never retried.
        _MODEL_LOAD_ATTEMPTED = True

        if not _MODEL_PATH.exists():
            # Query DB for settled trade count so operator knows how close they
            # are to the >=20 minimum needed to run --train.
            _settled_count = 0
            try:
                import sqlite3 as _sqlite3
                _conn = _sqlite3.connect(str(_DB_PATH))
                (_settled_count,) = _conn.execute(
                    "SELECT COUNT(*) FROM order_tracking "
                    "WHERE order_state='SETTLED' AND pnl IS NOT NULL"
                ).fetchone()
                _conn.close()
            except Exception:
                pass
            _training_ready = _settled_count >= 20
            logger.warning(
                "meta_gate_model_not_found",
                path=str(_MODEL_PATH),
                action="fail_open",
                settled_trades=_settled_count,
                training_ready=_training_ready,
                hint=(
                    "Run: python -m ml.meta_gate --train  to create the model."
                    if _training_ready else
                    f"Need {20 - _settled_count} more settled trades before training."
                ),
            )
            _MODEL_CACHE = False
            return None

        try:
            with open(_MODEL_PATH, "rb") as fh:
                bundle = pickle.load(fh)
            # Basic schema check
            required = {"model", "feature_names", "threshold"}
            missing = required - set(bundle.keys())
            if missing:
                raise ValueError(f"model bundle missing keys: {missing}")
            _MODEL_CACHE = bundle
            logger.info(
                "meta_gate_model_loaded",
                path=str(_MODEL_PATH),
                trained_at=bundle.get("trained_at", "unknown"),
                auc=bundle.get("metrics", {}).get("auc", "unknown"),
                threshold=bundle.get("threshold"),
            )
            return bundle
        except Exception as exc:
            logger.warning(
                "meta_gate_model_load_failed",
                path=str(_MODEL_PATH),
                error=str(exc),
                action="fail_open",
            )
            _MODEL_CACHE = False
            return None


def get_session_meta_gate_stats() -> Dict:
    """
    Return a snapshot of meta-gate approve/reject/error counts for the current
    session, suitable for check_session.py output.

    Counters are module-level ints incremented by should_trade().  They are
    never reset during a session; restart resets them via module re-import.
    """
    total = _meta_gate_approved + _meta_gate_rejected
    approve_rate = round(_meta_gate_approved / total, 4) if total > 0 else None
    return {
        "approved":    _meta_gate_approved,
        "rejected":    _meta_gate_rejected,
        "errors":      _meta_gate_errors,
        "shadow_decisions": _meta_gate_shadow_decisions,
        "shadow_rejections": _meta_gate_shadow_rejections,
        "shadow_fallbacks": _meta_gate_shadow_fallbacks,
        "shadow_feature_mismatches": _meta_gate_shadow_feature_mismatches,
        "shadow_load_successes": _meta_gate_shadow_load_successes,
        "shadow_load_failures": _meta_gate_shadow_load_failures,
        "shadow_load_failure_reasons": dict(_meta_gate_shadow_load_failure_reasons),
        "shadow_last_load_failure_reason": _meta_gate_shadow_last_load_failure_reason,
        "shadow_valid_promoted_bundle_decisions": _meta_gate_shadow_valid_promoted_bundle_decisions,
        "shadow_fallback_decisions_by_reason": dict(_meta_gate_shadow_fallback_decisions_by_reason),
        "shadow_schema_mismatch_decisions": _meta_gate_shadow_schema_mismatch_decisions,
        "shadow_artifact_load_successes": _meta_gate_shadow_load_successes,
        "shadow_artifact_load_failures": _meta_gate_shadow_load_failures,
        "shadow_scored_opportunities": _meta_gate_shadow_scored_opportunities,
        "shadow_unscored_opportunities": _meta_gate_shadow_unscored_opportunities,
        "shadow_artifact_load_status_summary": dict(_meta_gate_shadow_artifact_load_status_counts),
        "shadow_model_versions_observed": dict(_meta_gate_shadow_model_versions_observed),
        "shadow_feature_schema_versions_observed": dict(_meta_gate_shadow_feature_schema_versions_observed),
        "shadow_calibration_versions_observed": dict(_meta_gate_shadow_calibration_versions_observed),
        "shadow_selected_threshold_counts": dict(_meta_gate_shadow_selected_threshold_counts),
        "shadow_last_selected_threshold": _meta_gate_shadow_last_selected_threshold,
        "shadow_integrity_flags_observed": dict(_meta_gate_shadow_integrity_flags_observed),
        "shadow_block_reasons_observed": dict(_meta_gate_shadow_block_reasons_observed),
        "shadow_decision_mode_counts": dict(_meta_gate_shadow_decision_mode_counts),
        "shadow_effective_allow_trade_true": _meta_gate_shadow_effective_allow_trade_true,
        "shadow_effective_allow_trade_false": _meta_gate_shadow_effective_allow_trade_false,
        "shadow_shadow_only_true": _meta_gate_shadow_shadow_only_true,
        "shadow_shadow_only_false": _meta_gate_shadow_shadow_only_false,
        "shadow_p_profit_summary": _build_p_profit_summary(),
        "shadow_session_id": _meta_gate_shadow_session_id,
        "shadow_session_started_at": _meta_gate_shadow_session_started_at,
        "total":       total,
        "approve_rate": approve_rate,
    }


def _dict_to_array(features: dict, feature_names: List[str]) -> np.ndarray:
    """Convert feature dict to a (1, n_features) numpy array."""
    row = [float(features.get(name, 0.0)) for name in feature_names]
    return np.array([row], dtype=np.float32)


def _normalize_path_for_compare(value: object) -> str:
    return meta_promoted_contract.normalize_path_for_compare(value)


def _record_shadow_load_success(**fields: object) -> None:
    global _meta_gate_shadow_load_successes
    global _meta_gate_shadow_last_load_failure_reason
    _meta_gate_shadow_load_successes += 1
    _meta_gate_shadow_last_load_failure_reason = None
    logger.info("meta_gate_shadow_artifact_load_success", **fields)


def _record_shadow_load_failure(reason_code: str, **fields: object) -> None:
    global _meta_gate_shadow_load_failures
    global _meta_gate_shadow_last_load_failure_reason
    _meta_gate_shadow_load_failures += 1
    _meta_gate_shadow_last_load_failure_reason = str(reason_code)
    _meta_gate_shadow_load_failure_reasons[str(reason_code)] = (
        _meta_gate_shadow_load_failure_reasons.get(str(reason_code), 0) + 1
    )
    logger.warning(
        "meta_gate_shadow_artifact_load_failure",
        reason_code=str(reason_code),
        **fields,
    )


def _load_json_artifact(path: Path, *, reason_code: str) -> Dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ShadowArtifactValidationError(reason_code, f"{path.name} must contain a JSON object")
        return payload
    except FileNotFoundError as exc:
        raise ShadowArtifactValidationError(reason_code, f"missing artifact: {path}") from exc
    except ShadowArtifactValidationError:
        raise
    except Exception as exc:
        raise ShadowArtifactValidationError(reason_code, f"failed to read artifact {path}: {exc}") from exc


def _require_fields(payload: Dict[str, object], required_fields: List[str], *, reason_code: str, artifact_name: str) -> None:
    missing = meta_promoted_contract.find_missing_fields(payload, required_fields)
    if missing:
        raise ShadowArtifactValidationError(
            reason_code,
            f"{artifact_name} missing required fields: {', '.join(sorted(missing))}",
        )


def _validate_promotable_shadow_artifacts(bundle_path: Path, report_path: Path) -> Dict[str, object]:
    try:
        bundle = joblib.load(bundle_path)
    except FileNotFoundError as exc:
        raise ShadowArtifactValidationError("missing_artifact", f"missing promotable bundle: {bundle_path}") from exc
    except Exception as exc:
        raise ShadowArtifactValidationError("bundle_load_failed", f"failed to load promotable bundle: {exc}") from exc

    if not isinstance(bundle, dict):
        raise ShadowArtifactValidationError("bundle_not_dict", "promotable bundle must be a dict")

    _require_fields(
        bundle,
        list(meta_promoted_contract.PROMOTABLE_BUNDLE_REQUIRED_FIELDS),
        reason_code="missing_required_fields",
        artifact_name="promotable bundle",
    )

    bundle_contract_version = meta_promoted_contract.find_expected_string_mismatch(
        bundle,
        "contract_version",
        _PROMOTABLE_CONTRACT_VERSION,
    )
    if bundle_contract_version is not None:
        raise ShadowArtifactValidationError(
            "contract_version_mismatch",
            f"unsupported promotable bundle contract version: {bundle.get('contract_version')}",
        )
    bundle_pipeline_version = meta_promoted_contract.find_expected_string_mismatch(
        bundle,
        "pipeline_version",
        _PROMOTION_PIPELINE_VERSION,
    )
    if bundle_pipeline_version is not None:
        raise ShadowArtifactValidationError(
            "pipeline_version_mismatch",
            f"unsupported promotable bundle pipeline version: {bundle.get('pipeline_version')}",
        )
    if not hasattr(bundle.get("primary_model"), "predict_proba"):
        raise ShadowArtifactValidationError("missing_primary_model", "promotable bundle primary_model is invalid")
    if not hasattr(bundle.get("calibrator"), "predict_proba"):
        raise ShadowArtifactValidationError("missing_calibrator", "promotable bundle calibrator is invalid")

    report = _load_json_artifact(report_path, reason_code="missing_report")
    _require_fields(
        report,
        list(meta_promoted_contract.TRAINING_REPORT_REQUIRED_FIELDS),
        reason_code="missing_report_fields",
        artifact_name="training report",
    )
    report_contract_version = meta_promoted_contract.find_expected_string_mismatch(
        report,
        "contract_version",
        _PROMOTABLE_CONTRACT_VERSION,
    )
    if report_contract_version is not None:
        raise ShadowArtifactValidationError(
            "report_contract_version_mismatch",
            f"unsupported training report contract version: {report.get('contract_version')}",
        )
    report_pipeline_version = meta_promoted_contract.find_expected_string_mismatch(
        report,
        "pipeline_version",
        _PROMOTION_PIPELINE_VERSION,
    )
    if report_pipeline_version is not None:
        raise ShadowArtifactValidationError(
            "report_pipeline_version_mismatch",
            f"unsupported training report pipeline version: {report.get('pipeline_version')}",
        )

    promotion_gate = report.get("promotion_gate") or {}
    gate_version_mismatch = meta_promoted_contract.find_expected_string_mismatch(
        promotion_gate,
        "gate_version",
        _PROMOTION_GATE_VERSION,
    )
    if gate_version_mismatch is not None:
        raise ShadowArtifactValidationError(
            "gate_version_mismatch",
            f"unsupported promotion gate version: {promotion_gate.get('gate_version')}",
        )
    if bool(promotion_gate.get("passed")) is not True:
        raise ShadowArtifactValidationError("non_promotable_report", "training report is not promotable")

    integrity = report.get("integrity") or {}
    if bool(integrity.get("passed")) is not True:
        raise ShadowArtifactValidationError(
            "artifact_cross_reference_mismatch",
            f"training report integrity failed: {integrity.get('errors')}",
        )

    threshold_selection = report.get("threshold_selection") or {}
    if bool(threshold_selection.get("selected_before_test_evaluation")) is not True:
        raise ShadowArtifactValidationError(
            "threshold_selection_invalid",
            "threshold must be selected before test evaluation",
        )

    calibration = report.get("calibration") or {}
    calibration_fit_split_mismatch = meta_promoted_contract.find_expected_string_mismatch(
        calibration,
        "fit_split",
        meta_promoted_contract.VALIDATION_ONLY_FIT_SPLIT,
    )
    if calibration_fit_split_mismatch is not None:
        raise ShadowArtifactValidationError(
            "calibration_fit_split_invalid",
            f"unexpected calibration fit split: {calibration.get('fit_split')}",
        )

    expected_report_path = _normalize_path_for_compare(bundle.get("training_report_path"))
    actual_report_path = _normalize_path_for_compare(report_path)
    output_report_path = _normalize_path_for_compare((report.get("outputs") or {}).get("training_report_path"))
    if expected_report_path != actual_report_path or output_report_path != actual_report_path:
        raise ShadowArtifactValidationError(
            "report_path_mismatch",
            "training report path cross-reference mismatch",
        )

    output_bundle_path = _normalize_path_for_compare((report.get("outputs") or {}).get("promotable_model_bundle_path"))
    actual_bundle_path = _normalize_path_for_compare(bundle_path)
    if output_bundle_path != actual_bundle_path:
        raise ShadowArtifactValidationError(
            "bundle_path_mismatch",
            "promotable bundle path cross-reference mismatch",
        )

    staged_artifacts = report.get("staged_artifacts") or {}
    feature_schema_path = Path(str(bundle.get("staged_feature_schema_path") or ""))
    if _normalize_path_for_compare(staged_artifacts.get("feature_schema_path")) != _normalize_path_for_compare(feature_schema_path):
        raise ShadowArtifactValidationError(
            "feature_schema_path_mismatch",
            "feature schema path cross-reference mismatch",
        )
    feature_schema = _load_json_artifact(feature_schema_path, reason_code="missing_feature_schema")
    _require_fields(
        feature_schema,
        list(meta_promoted_contract.FEATURE_SCHEMA_REQUIRED_FIELDS),
        reason_code="missing_feature_schema_fields",
        artifact_name="feature schema",
    )

    feature_names = list(bundle.get("feature_names") or [])
    feature_names_in_order = list(feature_schema.get("feature_names_in_order") or [])
    if feature_names != feature_names_in_order:
        raise ShadowArtifactValidationError(
            "feature_names_mismatch",
            "feature names mismatch between promotable bundle and feature schema",
        )

    schema_hash = str(feature_schema.get("schema_hash") or "")
    if str(staged_artifacts.get("feature_schema_hash") or "") != schema_hash:
        raise ShadowArtifactValidationError(
            "schema_hash_mismatch",
            "feature schema hash mismatch between training report and feature schema",
        )

    feature_schema_version = str(feature_schema.get("feature_schema_version") or "")
    if (
        str(bundle.get("feature_schema_version") or "") != feature_schema_version
        or str(staged_artifacts.get("feature_schema_version") or "") != feature_schema_version
    ):
        raise ShadowArtifactValidationError(
            "feature_schema_version_mismatch",
            "feature schema version mismatch across promotable artifacts",
        )

    model_version = str(bundle.get("model_version") or "")
    if str(staged_artifacts.get("model_version") or "") != model_version:
        raise ShadowArtifactValidationError(
            "model_version_mismatch",
            "model version mismatch between promotable bundle and training report",
        )

    split_policy_version = str(bundle.get("split_policy_version") or "")
    split_policy_hash = str(bundle.get("split_policy_hash") or "")
    if (
        str(staged_artifacts.get("split_policy_version") or "") != split_policy_version
        or str(staged_artifacts.get("split_policy_hash") or "") != split_policy_hash
    ):
        raise ShadowArtifactValidationError(
            "split_policy_mismatch",
            "split policy mismatch between promotable bundle and training report",
        )

    calibration_version = str(calibration.get("method") or "")
    selected_threshold = float(bundle.get("selected_threshold"))

    return {
        "model": bundle["primary_model"],
        "calibrator": bundle["calibrator"],
        "feature_names": feature_names,
        "feature_defaults": dict(bundle.get("feature_defaults") or {}),
        "feature_schema_version": feature_schema_version,
        "schema_hash": schema_hash,
        "threshold": selected_threshold,
        "model_version": model_version,
        "calibration_version": calibration_version,
        "contract_version": str(bundle.get("contract_version") or ""),
        "split_policy_version": split_policy_version,
        "split_policy_hash": split_policy_hash,
        "training_report_path": str(report_path),
        "promotable": True,
    }


def _build_model_version(model_bundle: dict) -> str:
    explicit_version = str(model_bundle.get("model_version") or "").strip()
    if explicit_version:
        return explicit_version
    trained_at = str(model_bundle.get("trained_at") or "").strip()
    if trained_at:
        return f"trained_at:{trained_at}"
    metrics = model_bundle.get("metrics") or {}
    auc = metrics.get("auc")
    if auc not in {None, ""}:
        return f"legacy_auc:{auc}"
    return "unknown_model_version"


def _get_shadow_runtime_model() -> Optional[dict]:
    global _SHADOW_MODEL_CACHE, _SHADOW_MODEL_LOAD_ATTEMPTED

    if _SHADOW_MODEL_LOAD_ATTEMPTED:
        return None if (_SHADOW_MODEL_CACHE is False or _SHADOW_MODEL_CACHE is _NOT_LOADED) else _SHADOW_MODEL_CACHE

    with _shadow_model_load_lock:
        if _SHADOW_MODEL_LOAD_ATTEMPTED:
            return None if (_SHADOW_MODEL_CACHE is False or _SHADOW_MODEL_CACHE is _NOT_LOADED) else _SHADOW_MODEL_CACHE

        _SHADOW_MODEL_LOAD_ATTEMPTED = True

        if not _PROMOTABLE_MODEL_BUNDLE_PATH.exists():
            _record_shadow_load_failure(
                "missing_artifact",
                bundle_path=str(_PROMOTABLE_MODEL_BUNDLE_PATH),
                report_path=str(_PROMOTION_REPORT_PATH),
                action="fallback_no_model",
            )
            _SHADOW_MODEL_CACHE = False
            return None

        if not _PROMOTION_REPORT_PATH.exists():
            _record_shadow_load_failure(
                "missing_report",
                bundle_path=str(_PROMOTABLE_MODEL_BUNDLE_PATH),
                report_path=str(_PROMOTION_REPORT_PATH),
                action="fallback_no_model",
            )
            _SHADOW_MODEL_CACHE = False
            return None

        try:
            normalized_bundle = _validate_promotable_shadow_artifacts(
                _PROMOTABLE_MODEL_BUNDLE_PATH,
                _PROMOTION_REPORT_PATH,
            )
            _SHADOW_MODEL_CACHE = normalized_bundle
            _record_shadow_load_success(
                bundle_path=str(_PROMOTABLE_MODEL_BUNDLE_PATH),
                report_path=str(_PROMOTION_REPORT_PATH),
                model_version=normalized_bundle.get("model_version"),
                feature_schema_version=normalized_bundle.get("feature_schema_version"),
                calibration_version=normalized_bundle.get("calibration_version"),
                contract_version=normalized_bundle.get("contract_version"),
                threshold=normalized_bundle.get("threshold"),
                action="shadow_scoring_enabled",
            )
            return normalized_bundle
        except ShadowArtifactValidationError as exc:
            _record_shadow_load_failure(
                exc.reason_code,
                bundle_path=str(_PROMOTABLE_MODEL_BUNDLE_PATH),
                report_path=str(_PROMOTION_REPORT_PATH),
                error=str(exc),
                action="fallback_no_model",
            )
            _SHADOW_MODEL_CACHE = False
            return None


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_notes_field(notes: str, field: str, default: float) -> float:
    """
    Extract a float from the reason string stored in order_tracking.notes.

    The notes field contains strings like:
      "charlie_signal side=YES p_win=0.612 implied=0.500 edge=0.094 conf=0.750 ..."
    We normalise 'edge' and 'implied' from the stored reason.
    """
    # Map display name → aliases used in notes
    aliases = {
        "edge":         ["edge=", "net_edge="],
        "implied":      ["implied=", "implied_prob="],
        "fee":          ["fee="],
        "tech_regime":  ["tech_regime=", "technical_regime="],
    }
    for alias in aliases.get(field, [f"{field}="]):
        idx = notes.find(alias)
        if idx == -1:
            continue
        start = idx + len(alias)
        end = notes.find(" ", start)
        token = notes[start:] if end == -1 else notes[start:end]
        return _safe_float(token.strip(), default)
    return default


def _compute_ece(proba: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error (ECE)."""
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    n = len(labels)
    for i in range(n_bins):
        mask = (proba >= bins[i]) & (proba < bins[i + 1])
        if not mask.any():
            continue
        bin_mean_conf = float(proba[mask].mean())
        bin_acc = float(labels[mask].mean())
        ece += (mask.sum() / n) * abs(bin_mean_conf - bin_acc)
    return ece


# ---------------------------------------------------------------------------
# CLI entry-point (training only — not reachable from the hot path)
# ---------------------------------------------------------------------------


def _main():
    # structlog auto-configures on first use; no basicConfig needed.
    parser = argparse.ArgumentParser(description="Train the meta-gate classifier.")
    parser.add_argument("--train",  action="store_true", help="Train and persist model.")
    parser.add_argument("--db",     default=str(_DB_PATH), help="Path to trading.db.")
    parser.add_argument("--model",  default=str(_MODEL_PATH), help="Output model path.")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD,
                        help="Classification threshold (default=0.50).")
    parser.add_argument("--lgbm",   action="store_true",
                        help="Use LightGBM instead of Logistic Regression.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print dataset stats without training.")
    args = parser.parse_args()

    if args.dry_run:
        X, y, fnames = build_training_dataset(args.db)
        print(f"Samples: {len(y)}  Positive (win): {y.sum()}  Negative: {(1-y).sum()}")
        print(f"Features: {fnames}")
        return

    if args.train:
        train_and_persist(
            db_path=args.db,
            model_path=args.model,
            threshold=args.threshold,
            use_lgbm=args.lgbm,
        )
        return

    parser.print_help()


if __name__ == "__main__":
    _main()
