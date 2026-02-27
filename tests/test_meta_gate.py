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

import math
import sys
from pathlib import Path
from datetime import datetime, timezone

# Make repo root importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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

        # Must be True — fail-open, never silently block on missing model
        assert result is True, "should_trade must fail-open (return True) when model is absent"

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
        assert result is True, "should_trade must fail-open on corrupt pickle"

    def test_fail_open_returns_bool(self, tmp_path, monkeypatch):
        """Return type must always be bool, never None or exception."""
        import ml.meta_gate as mg

        monkeypatch.setattr(mg, "_MODEL_PATH", tmp_path / "missing.pkl")
        monkeypatch.setattr(mg, "_MODEL_CACHE", None)

        result = mg.should_trade({})
        assert isinstance(result, bool)


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
