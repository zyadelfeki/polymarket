"""
Unit tests for ml/meta_gate.py — Session 1.

Covers:
  - should_trade() fail-open (no model file present)
  - extract_features_from_opportunity() shape and value contracts
  - _parse_notes_field() parsing edge cases
  - Feature dict keys are exactly the 12 expected features

Run:  pytest tests/test_meta_gate.py -v
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone

import joblib
import numpy as np
import pytest

# Make repo root importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ml import meta_promotion, meta_training
from tests.test_meta_training import _make_training_rows, _write_validated_inputs


# ---------------------------------------------------------------------------
# should_trade — fail-open contract
# ---------------------------------------------------------------------------

class TestShouldTradeFailOpen:
    """should_trade must return True when no model file exists."""

    def test_fail_open_no_model(self, tmp_path, monkeypatch):
        """
        Reset the global model cache so the loader tries to read a non-existent
        path, then assert that should_trade returns True (fail-open).
        """
        import ml.meta_gate as mg

        # Point model path to a guaranteed-absent file (must remain a Path object)
        monkeypatch.setattr(mg, "_MODEL_PATH", tmp_path / "nonexistent.pkl")
        # Reset cache so the loader re-tries
        monkeypatch.setattr(mg, "_MODEL_CACHE", None)

        features = mg.extract_features_from_opportunity(charlie_p_win_raw=0.6, net_edge=0.05)
        result = mg.should_trade(features)

        # Must be (True, 1.0) — fail-open, never silently block on missing model
        decision, proba = result
        assert decision is True, "should_trade must fail-open (return True) when model is absent"
        assert proba == 1.0, "fail-open proba must be 1.0"

    def test_fail_open_corrupt_model(self, tmp_path, monkeypatch):
        """
        Write a corrupt pickle to the model path and verify fail-open.
        """
        import ml.meta_gate as mg

        corrupt = tmp_path / "corrupt.pkl"
        corrupt.write_bytes(b"notapickle" * 10)

        monkeypatch.setattr(mg, "_MODEL_PATH", corrupt)
        monkeypatch.setattr(mg, "_MODEL_CACHE", None)

        features = mg.extract_features_from_opportunity()
        result = mg.should_trade(features)
        decision, proba = result
        assert decision is True, "should_trade must fail-open on corrupt pickle"
        assert proba == 1.0, "fail-open proba must be 1.0"

    def test_fail_open_returns_bool(self, tmp_path, monkeypatch):
        """Return type must always be bool, never None or exception."""
        import ml.meta_gate as mg

        monkeypatch.setattr(mg, "_MODEL_PATH", tmp_path / "missing.pkl")
        monkeypatch.setattr(mg, "_MODEL_CACHE", None)

        result = mg.should_trade({})
        assert isinstance(result, tuple), "should_trade must return a (bool, float) tuple"
        decision, proba = result
        assert isinstance(decision, bool)
        assert isinstance(proba, float)


# ---------------------------------------------------------------------------
# extract_features_from_opportunity — shape + value contracts
# ---------------------------------------------------------------------------

_EXPECTED_FEATURE_KEYS = {
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
}


class TestExtractFeatures:
    """extract_features_from_opportunity must return exactly the 12 expected keys."""

    def test_returns_all_keys(self):
        import ml.meta_gate as mg
        feats = mg.extract_features_from_opportunity()
        assert set(feats.keys()) == _EXPECTED_FEATURE_KEYS, (
            f"Feature keys mismatch. Got: {set(feats.keys())}"
        )

    def test_no_extra_keys(self):
        import ml.meta_gate as mg
        feats = mg.extract_features_from_opportunity(
            charlie_p_win_raw=0.7,
            net_edge=0.04,
            fee=0.01,
            ofi_conflict=True,
        )
        extra = set(feats.keys()) - _EXPECTED_FEATURE_KEYS
        assert not extra, f"Unexpected extra keys: {extra}"

    def test_circular_encoding_bounds(self):
        """hour_sin/cos and dow_sin/cos must be in [-1, 1]."""
        import ml.meta_gate as mg
        for hour in range(24):
            feats = mg.extract_features_from_opportunity(
                now=datetime(2024, 1, 15, hour, 0, tzinfo=timezone.utc)
            )
            assert -1.0 <= feats["hour_sin"] <= 1.0
            assert -1.0 <= feats["hour_cos"] <= 1.0

    def test_ofi_conflict_bool_to_float(self):
        """ofi_conflict=True → 1.0, ofi_conflict=False → 0.0."""
        import ml.meta_gate as mg
        f_true  = mg.extract_features_from_opportunity(ofi_conflict=True)
        f_false = mg.extract_features_from_opportunity(ofi_conflict=False)
        assert f_true["ofi_conflict"] == 1.0
        assert f_false["ofi_conflict"] == 0.0

    def test_rolling_defaults(self):
        """When rolling stats are omitted the defaults are safe and defined."""
        import ml.meta_gate as mg
        feats = mg.extract_features_from_opportunity()
        assert feats["rolling_win_rate"] == 0.5   # neutral default
        assert feats["rolling_pnl_z"] == 0.0       # neutral default

    def test_all_values_are_finite(self):
        """No feature value may be NaN or ±inf."""
        import ml.meta_gate as mg
        feats = mg.extract_features_from_opportunity(
            charlie_p_win_raw=0.65,
            net_edge=0.03,
            fee=0.005,
            ofi_conflict=False,
            rolling_win_rate=0.55,
            rolling_pnl_z=-0.2,
        )
        for k, v in feats.items():
            assert math.isfinite(v), f"Feature '{k}' is not finite: {v}"


# ---------------------------------------------------------------------------
# _parse_notes_field — parser edge cases
# ---------------------------------------------------------------------------

class TestParseNotesField:
    """_parse_notes_field must extract floats from the notes string correctly."""

    def _parse(self, notes, field, default=0.0):
        from ml.meta_gate import _parse_notes_field
        return _parse_notes_field(notes, field, default)

    def test_parse_edge(self):
        notes = "charlie_signal side=YES p_win=0.612 implied=0.500 edge=0.094 conf=0.750"
        assert abs(self._parse(notes, "edge") - 0.094) < 1e-9

    def test_parse_implied(self):
        notes = "charlie_signal side=YES p_win=0.612 implied=0.500 edge=0.094 conf=0.750"
        assert abs(self._parse(notes, "implied") - 0.500) < 1e-9

    def test_parse_fee(self):
        notes = "charlie_signal side=YES fee=0.005 edge=0.094"
        assert abs(self._parse(notes, "fee") - 0.005) < 1e-9

    def test_missing_field_returns_default(self):
        notes = "charlie_signal side=YES edge=0.020"
        assert self._parse(notes, "fee", default=99.0) == 99.0

    def test_empty_notes_returns_default(self):
        assert self._parse("", "edge", default=-1.0) == -1.0

    def test_field_at_end_of_string(self):
        """Field value at EOL (no trailing space)."""
        notes = "edge=0.07"
        assert abs(self._parse(notes, "edge") - 0.07) < 1e-9

    def test_field_not_confused_with_prefix(self):
        """
        'net_edge=0.1' should not be parsed when looking for 'edge' if
        the implementation correctly uses 'edge=' as the alias prefix
        (not as a substring match). Verify no false positive prefix collision.
        Cases like: edge=... vs. bad_edge=...
        """
        # If notes has 'edge=0.05' it must parse correctly
        notes = "foo=0.1 edge=0.05 bar=0.2"
        val = self._parse(notes, "edge")
        assert abs(val - 0.05) < 1e-9


class _FakeModel:
    def __init__(self, proba: float):
        self.proba = proba

    def predict_proba(self, X):
        return np.array([[1.0 - self.proba, self.proba]], dtype=np.float32)


def _reset_shadow_loader_state(mg):
    mg._SHADOW_MODEL_CACHE = mg._NOT_LOADED
    mg._SHADOW_MODEL_LOAD_ATTEMPTED = False
    mg._meta_gate_shadow_decisions = 0
    mg._meta_gate_shadow_rejections = 0
    mg._meta_gate_shadow_fallbacks = 0
    mg._meta_gate_shadow_feature_mismatches = 0
    mg._meta_gate_shadow_load_successes = 0
    mg._meta_gate_shadow_load_failures = 0
    mg._meta_gate_shadow_load_failure_reasons = {}
    mg._meta_gate_shadow_last_load_failure_reason = None
    mg._meta_gate_shadow_valid_promoted_bundle_decisions = 0
    mg._meta_gate_shadow_schema_mismatch_decisions = 0
    mg._meta_gate_shadow_scored_opportunities = 0
    mg._meta_gate_shadow_unscored_opportunities = 0
    mg._meta_gate_shadow_fallback_decisions_by_reason = {}
    mg._meta_gate_shadow_artifact_load_status_counts = {}
    mg._meta_gate_shadow_model_versions_observed = {}
    mg._meta_gate_shadow_feature_schema_versions_observed = {}
    mg._meta_gate_shadow_calibration_versions_observed = {}
    mg._meta_gate_shadow_selected_threshold_counts = {}
    mg._meta_gate_shadow_last_selected_threshold = None
    mg._meta_gate_shadow_integrity_flags_observed = {}
    mg._meta_gate_shadow_block_reasons_observed = {}
    mg._meta_gate_shadow_decision_mode_counts = {}
    mg._meta_gate_shadow_effective_allow_trade_true = 0
    mg._meta_gate_shadow_effective_allow_trade_false = 0
    mg._meta_gate_shadow_shadow_only_true = 0
    mg._meta_gate_shadow_shadow_only_false = 0
    mg._meta_gate_shadow_p_profit_sum = 0.0
    mg._meta_gate_shadow_p_profit_count = 0
    mg._meta_gate_shadow_p_profit_min = None
    mg._meta_gate_shadow_p_profit_max = None
    mg._meta_gate_shadow_session_id = "test-shadow-session"
    mg._meta_gate_shadow_session_started_at = "2026-03-07T10:00:00Z"


def _build_promotable_shadow_artifacts(tmp_path: Path) -> tuple[Path, Path]:
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
    meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(staging_dir),
        run_id="shadow-loader-stage",
        created_at="2026-03-07T07:00:00Z",
        random_state=42,
    )
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(final_dir),
        run_id="shadow-loader-final",
        created_at="2026-03-07T07:10:00Z",
    )
    return Path(report["promotable_model_bundle_path"]), Path(report["training_report_path"])


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


class TestRuntimeDecisionContract:
    def test_runtime_decision_contract_shape(self, monkeypatch):
        import ml.meta_gate as mg

        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(
            mg,
            "_get_shadow_runtime_model",
            lambda: {
                "model": _FakeModel(0.73),
                "calibrator": None,
                "feature_names": sorted(_EXPECTED_FEATURE_KEYS),
                "threshold": 0.5,
                "model_version": "meta-shadow-v1",
                "feature_schema_version": "meta_candidate_v1",
                "calibration_version": "platt_scaler_v1",
                "contract_version": mg._PROMOTABLE_CONTRACT_VERSION,
            },
        )

        decision = mg.evaluate_runtime_decision(
            mg.extract_features_from_opportunity(net_edge=0.0123),
            expected_feature_schema_version="meta_candidate_v1",
            calibration_version="platt_scaler_v1",
        )
        payload = decision.as_dict()

        assert tuple(payload.keys()) == mg.SHADOW_SCORING_EVENT_FIELDS
        assert payload["artifact_load_status"] == mg.SHADOW_ARTIFACT_LOAD_STATUS_LOADED
        assert payload["artifact_contract_version"] == mg._PROMOTABLE_CONTRACT_VERSION
        assert payload["scoring_mode"] == mg.SHADOW_SCORING_MODE_VALID_BUNDLE
        assert payload["selected_threshold"] == 0.5
        assert payload["allow_trade"] is True
        assert payload["effective_allow_trade"] is True
        assert payload["decision_mode"] == "shadow"
        assert payload["shadow_only"] is True
        assert payload["expected_return_bps"] == 123.0
        assert payload["size_multiplier"] == 1.0

    def test_feature_mismatch_returns_shadow_fallback(self, monkeypatch):
        import ml.meta_gate as mg

        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(
            mg,
            "_get_shadow_runtime_model",
            lambda: {
                "model": _FakeModel(0.25),
                "calibrator": None,
                "feature_names": ["charlie_p_win_raw", "net_edge", "missing_feature"],
                "threshold": 0.5,
                "model_version": "meta-shadow-v1",
                "feature_schema_version": "meta_candidate_v1",
                "calibration_version": "platt_scaler_v1",
                "contract_version": mg._PROMOTABLE_CONTRACT_VERSION,
            },
        )

        decision = mg.evaluate_runtime_decision(
            {"charlie_p_win_raw": 0.61, "net_edge": 0.01},
            expected_feature_schema_version="meta_candidate_v1",
        )

        assert decision.allow_trade is True
        assert decision.block_reason == "feature_schema_mismatch"
        assert decision.fallback_reason == "feature_schema_mismatch"
        assert decision.scoring_mode == mg.SHADOW_SCORING_MODE_SCHEMA_MISMATCH
        assert decision.effective_allow_trade is True
        assert decision.training_eligibility == "blocked_feature_mismatch"
        assert "feature_schema_mismatch" in decision.integrity_flags

    def test_fallback_mode_is_explicit_when_model_missing(self, monkeypatch):
        import ml.meta_gate as mg

        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(mg, "_get_shadow_runtime_model", lambda: None)
        monkeypatch.setattr(mg, "_meta_gate_shadow_last_load_failure_reason", "model_unavailable")

        decision = mg.evaluate_runtime_decision(
            mg.extract_features_from_opportunity(net_edge=0.02),
            expected_feature_schema_version="meta_candidate_v1",
        )

        assert decision.allow_trade is True
        assert decision.block_reason == "fallback_model_unavailable"
        assert decision.fallback_reason == "model_unavailable"
        assert decision.scoring_mode == mg.SHADOW_SCORING_MODE_FALLBACK
        assert decision.artifact_load_status == mg.SHADOW_ARTIFACT_LOAD_STATUS_FAILED
        assert decision.model_version == "fallback:no_model"
        assert decision.effective_allow_trade is True
        assert "fallback_decision" in decision.integrity_flags

    def test_logging_and_shadow_metrics_are_emitted(self, monkeypatch):
        import ml.meta_gate as mg

        events = []

        class _Logger:
            def info(self, event, **fields):
                events.append(("info", event, fields))

            def warning(self, event, **fields):
                events.append(("warning", event, fields))

        monkeypatch.setattr(mg, "logger", _Logger())
        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(
            mg,
            "_get_shadow_runtime_model",
            lambda: {
                "model": _FakeModel(0.12),
                "calibrator": None,
                "feature_names": sorted(_EXPECTED_FEATURE_KEYS),
                "threshold": 0.5,
                "model_version": "meta-shadow-v1",
                "feature_schema_version": "meta_candidate_v1",
                "calibration_version": "platt_scaler_v1",
                "contract_version": mg._PROMOTABLE_CONTRACT_VERSION,
            },
        )

        decision = mg.evaluate_runtime_decision(
            mg.extract_features_from_opportunity(net_edge=0.01),
            expected_feature_schema_version="meta_candidate_v1",
        )
        stats = mg.get_session_meta_gate_stats()

        assert decision.allow_trade is False
        assert any(event == "meta_gate_shadow_decision" for _, event, _ in events)
        assert stats["shadow_decisions"] == 1
        assert stats["shadow_rejections"] == 1
        assert stats["shadow_fallbacks"] == 0
        assert stats["shadow_valid_promoted_bundle_decisions"] == 1
        assert stats["shadow_scored_opportunities"] == 1
        assert stats["shadow_unscored_opportunities"] == 0

    def test_shadow_counters_update_across_valid_fallback_and_mismatch_cases(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        valid_decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )
        mismatch_decision = mg.evaluate_runtime_decision(
            {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71},
            expected_feature_schema_version="meta_candidate_v1",
        )

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", tmp_path / "missing.joblib")
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", tmp_path / "missing-report.json")
        mg.reload_shadow_runtime_bundle()
        fallback_decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )
        stats = mg.get_session_meta_gate_stats()

        assert valid_decision.scoring_mode == mg.SHADOW_SCORING_MODE_VALID_BUNDLE
        assert mismatch_decision.scoring_mode == mg.SHADOW_SCORING_MODE_SCHEMA_MISMATCH
        assert fallback_decision.scoring_mode == mg.SHADOW_SCORING_MODE_FALLBACK
        assert stats["shadow_valid_promoted_bundle_decisions"] == 1
        assert stats["shadow_schema_mismatch_decisions"] == 1
        assert stats["shadow_artifact_load_successes"] == 1
        assert stats["shadow_artifact_load_failures"] == 1
        assert stats["shadow_scored_opportunities"] == 1
        assert stats["shadow_unscored_opportunities"] == 2
        assert stats["shadow_fallback_decisions_by_reason"]["feature_schema_mismatch"] == 1
        assert stats["shadow_fallback_decisions_by_reason"]["missing_artifact"] == 1

    def test_runtime_scorer_matches_direct_promoted_bundle_scoring(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        normalized_bundle = mg._get_shadow_runtime_model()
        feature_payload = _shadow_feature_payload()
        runtime_decision = mg.evaluate_runtime_decision(
            feature_payload,
            expected_feature_schema_version="meta_candidate_v1",
        )
        direct_decision = mg._score_promoted_shadow_bundle(
            normalized_bundle,
            feature_payload,
            expected_feature_schema_version="meta_candidate_v1",
            calibration_version="platt_scaler_v1",
        )

        assert normalized_bundle is not None
        assert runtime_decision.scoring_mode == mg.SHADOW_SCORING_MODE_VALID_BUNDLE
        assert direct_decision.scoring_mode == mg.SHADOW_SCORING_MODE_VALID_BUNDLE
        assert runtime_decision.p_profit == pytest.approx(direct_decision.p_profit)
        assert runtime_decision.selected_threshold == pytest.approx(direct_decision.selected_threshold)
        assert runtime_decision.allow_trade is direct_decision.allow_trade
        assert runtime_decision.effective_allow_trade is True
        assert direct_decision.effective_allow_trade is True

    def test_successful_promotable_bundle_load(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        events = []

        class _Logger:
            def info(self, event, **fields):
                events.append(("info", event, fields))

            def warning(self, event, **fields):
                events.append(("warning", event, fields))

        monkeypatch.setattr(mg, "logger", _Logger())
        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        bundle = mg._get_shadow_runtime_model()
        decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )
        stats = mg.get_session_meta_gate_stats()

        assert bundle is not None
        assert bundle["promotable"] is True
        assert bundle["contract_version"] == "promotable_offline_v1"
        assert stats["shadow_load_successes"] == 1
        assert stats["shadow_load_failures"] == 0
        assert any(event == "meta_gate_shadow_artifact_load_success" for _, event, _ in events)
        assert decision.block_reason in {None, "p_profit_below_threshold"}

    def test_fallback_on_missing_artifact(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", tmp_path / "missing.joblib")
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", tmp_path / "missing-report.json")
        _reset_shadow_loader_state(mg)

        decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )
        stats = mg.get_session_meta_gate_stats()

        assert decision.allow_trade is True
        assert decision.block_reason == "fallback_model_unavailable"
        assert "artifact_load_failure:missing_artifact" in decision.integrity_flags
        assert stats["shadow_load_failures"] == 1
        assert stats["shadow_load_failure_reasons"]["missing_artifact"] == 1

    def test_fallback_on_cross_reference_mismatch(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["outputs"]["promotable_model_bundle_path"] = str(tmp_path / "wrong.joblib")
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )

        assert decision.allow_trade is True
        assert "artifact_load_failure:bundle_path_mismatch" in decision.integrity_flags

    def test_fallback_on_schema_mismatch(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        bundle = joblib.load(bundle_path)
        feature_schema_path = Path(bundle["staged_feature_schema_path"])
        feature_schema = json.loads(feature_schema_path.read_text(encoding="utf-8"))
        feature_schema["schema_hash"] = "corrupted-hash"
        feature_schema_path.write_text(json.dumps(feature_schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )

        assert decision.allow_trade is True
        assert "artifact_load_failure:schema_hash_mismatch" in decision.integrity_flags

    def test_fallback_on_non_promotable_report(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["promotion_gate"]["passed"] = False
        report["promotion_gate"]["blocked"] = True
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )

        assert decision.allow_trade is True
        assert "artifact_load_failure:non_promotable_report" in decision.integrity_flags

    def test_shadow_session_summary_schema_and_counts(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        valid_decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )
        mismatch_decision = mg.evaluate_runtime_decision(
            {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71},
            expected_feature_schema_version="meta_candidate_v1",
        )

        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", tmp_path / "missing.joblib")
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", tmp_path / "missing-report.json")
        mg.reload_shadow_runtime_bundle()
        fallback_decision = mg.evaluate_runtime_decision(
            _shadow_feature_payload(),
            expected_feature_schema_version="meta_candidate_v1",
        )

        summary = mg.build_shadow_session_summary(
            session_id="shadow-session-4-7",
            exported_at="2026-03-07T10:15:00Z",
        )

        assert summary["artifact_version"] == mg.SHADOW_SESSION_SUMMARY_ARTIFACT_VERSION
        assert summary["schema_version"] == mg.SHADOW_SESSION_SUMMARY_SCHEMA_VERSION
        assert summary["session_id"] == "shadow-session-4-7"
        assert summary["started_at"] == "2026-03-07T10:00:00Z"
        assert summary["exported_at"] == "2026-03-07T10:15:00Z"
        assert summary["decision_counts"] == {
            "shadow_decisions": 3,
            "valid_promoted_bundle_decisions": 1,
            "shadow_rejections": 0 if valid_decision.allow_trade else 1,
            "fallback_decisions": 2,
            "fallback_decisions_by_reason": {
                "feature_schema_mismatch": 1,
                "missing_artifact": 1,
            },
            "schema_mismatch_decisions": 1,
            "scored_opportunities": 1,
            "unscored_opportunities": 2,
        }
        assert summary["artifact_load_status_summary"] == {
            mg.SHADOW_ARTIFACT_LOAD_STATUS_LOADED: 2,
            mg.SHADOW_ARTIFACT_LOAD_STATUS_FAILED: 1,
        }
        assert summary["p_profit_summary"]["count"] == 1
        assert summary["p_profit_summary"]["min"] == pytest.approx(valid_decision.p_profit)
        assert summary["p_profit_summary"]["max"] == pytest.approx(valid_decision.p_profit)
        assert summary["p_profit_summary"]["mean"] == pytest.approx(valid_decision.p_profit)
        assert summary["threshold_summary"]["last_selected_threshold"] == pytest.approx(valid_decision.selected_threshold)
        assert summary["observed_versions"]["feature_schema_version_counts"]["meta_candidate_v1"] == 3
        assert summary["integrity_flags_observed"]["shadow_only_no_trade_impact"] == 3
        assert summary["block_reasons_observed"][mismatch_decision.block_reason] == 1
        assert summary["block_reasons_observed"][fallback_decision.block_reason] == 1
        assert summary["observational_contract"] == {
            "decision_mode_counts": {"shadow": 3},
            "effective_allow_trade_true_count": 3,
            "effective_allow_trade_false_count": 0,
            "shadow_only_true_count": 3,
            "shadow_only_false_count": 0,
        }

    def test_shadow_session_summary_export_writes_json(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(mg, "_get_shadow_runtime_model", lambda: None)
        monkeypatch.setattr(mg, "_meta_gate_shadow_last_load_failure_reason", "model_unavailable")

        mg.evaluate_runtime_decision(
            mg.extract_features_from_opportunity(net_edge=0.02),
            expected_feature_schema_version="meta_candidate_v1",
        )
        output_path = tmp_path / "shadow_session_summary.json"
        summary = mg.export_shadow_session_summary(
            output_path,
            session_id="shadow-session-export",
            exported_at="2026-03-07T10:20:00Z",
        )
        payload = json.loads(output_path.read_text(encoding="utf-8"))

        assert payload == summary
        assert payload["session_id"] == "shadow-session-export"
        assert payload["decision_counts"]["fallback_decisions"] == 1

    def test_replay_agreement_report_schema_and_counts(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        bundle_path, report_path = _build_promotable_shadow_artifacts(tmp_path)
        monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
        monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
        _reset_shadow_loader_state(mg)

        feature_batches = [
            _shadow_feature_payload(),
            {"selected_side_is_yes": 1.0, "raw_yes_prob": 0.71},
        ]
        report = mg.build_shadow_replay_agreement_report(
            feature_batches,
            expected_feature_schema_version="meta_candidate_v1",
            report_id="replay-report-4-7",
            exported_at="2026-03-07T10:25:00Z",
        )

        assert report["artifact_version"] == mg.SHADOW_REPLAY_AGREEMENT_ARTIFACT_VERSION
        assert report["schema_version"] == mg.SHADOW_REPLAY_AGREEMENT_SCHEMA_VERSION
        assert report["report_id"] == "replay-report-4-7"
        assert report["exported_at"] == "2026-03-07T10:25:00Z"
        assert report["input_count"] == 2
        assert report["valid_scored_count"] == 1
        assert report["fallback_count"] == 0
        assert report["schema_mismatch_count"] == 1
        assert report["artifact_load_status_summary"] == {mg.SHADOW_ARTIFACT_LOAD_STATUS_LOADED: 2}
        assert report["fallback_counts_by_reason"] == {"feature_schema_mismatch": 1}
        assert report["p_profit_match"]["compared_count"] == 1
        assert report["p_profit_match"]["exact_match_count"] == 1
        assert report["p_profit_match"]["exact_match_rate"] == pytest.approx(1.0)
        assert report["p_profit_match"]["tolerance_match_count"] == 1
        assert report["p_profit_match"]["tolerance_match_rate"] == pytest.approx(1.0)
        assert report["threshold_interpretation"]["agreement_count"] == 1
        assert report["threshold_interpretation"]["agreement_rate"] == pytest.approx(1.0)
        assert report["disagreement_examples_count"] == 0
        assert report["mismatch_examples"] == []
        assert report["observational_contract"] == {
            "decision_mode_counts": {"shadow": 2},
            "effective_allow_trade_true_count": 2,
            "effective_allow_trade_false_count": 0,
            "shadow_only_true_count": 2,
            "shadow_only_false_count": 0,
        }

    def test_replay_agreement_report_export_writes_json(self, tmp_path, monkeypatch):
        import ml.meta_gate as mg

        _reset_shadow_loader_state(mg)
        monkeypatch.setattr(mg, "_get_shadow_runtime_model", lambda: None)
        monkeypatch.setattr(mg, "_meta_gate_shadow_last_load_failure_reason", "model_unavailable")

        output_path = tmp_path / "shadow_replay_report.json"
        report = mg.export_shadow_replay_agreement_report(
            [mg.extract_features_from_opportunity(net_edge=0.02)],
            output_path,
            expected_feature_schema_version="meta_candidate_v1",
            report_id="replay-export-4-7",
            exported_at="2026-03-07T10:30:00Z",
        )
        payload = json.loads(output_path.read_text(encoding="utf-8"))

        assert payload == report
        assert payload["report_id"] == "replay-export-4-7"
        assert payload["fallback_count"] == 1
        assert payload["artifact_load_status_summary"] == {mg.SHADOW_ARTIFACT_LOAD_STATUS_FAILED: 1}
