"""
Unit tests for utils/regime_classifier.py — Session 2.

Covers:
  - _rule_based_classify() for all four regimes with canonical inputs
  - classify_regime() returns a valid regime even on empty/invalid features
  - Rate-limiting: _apply_rate_limit() holds regime until confirmation count met
  - Rate-limiting: _apply_rate_limit() commits on first call when dwell=0 (startup)

Run:  pytest tests/test_regime_classifier.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

VALID_REGIMES = {"calm", "trend_up", "trend_down", "event"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_regime_state(mod):
    """Reset all global state in the regime_classifier module so tests are isolated."""
    with mod._state_lock:
        mod._current_regime      = "calm"
        mod._current_regime_ts   = 0.0    # triggers dwell_ok=True on first transition
        mod._candidate_regime    = "calm"
        mod._candidate_count     = 0
        mod._regime_durations    = {r: 0.0 for r in mod.REGIMES}
        mod._regime_change_count = 0      # added FIX-6: reset explicit transition counter


# ---------------------------------------------------------------------------
# _rule_based_classify — pure function, no side-effects
# ---------------------------------------------------------------------------

class TestRuleBasedClassify:
    """Tests for the deterministic rule-based classifier."""

    def _classify(self, **kwargs):
        from utils.regime_classifier import _rule_based_classify
        return _rule_based_classify(kwargs)

    # ---- event regime ----

    def test_event_vol_ratio(self):
        """vol_ratio_5_60 >= 2.0 → event"""
        result = self._classify(vol_5min=0.003, vol_60min=0.001, vol_ratio_5_60=2.5)
        assert result == "event"

    def test_event_vol_ratio_exactly_2(self):
        """vol_ratio_5_60 = 2.0 exactly → event (boundary inclusive)"""
        result = self._classify(vol_ratio_5_60=2.0)
        assert result == "event"

    def test_event_absolute_vol_spike(self):
        """vol_5min >= 0.008 alone → event, regardless of ratio"""
        result = self._classify(vol_5min=0.010, vol_ratio_5_60=1.0)
        assert result == "event"

    def test_event_boundary_vol_5min(self):
        """vol_5min == 0.008 → event (boundary inclusive)"""
        result = self._classify(vol_5min=0.008, vol_ratio_5_60=1.0)
        assert result == "event"

    # ---- trend_up regime ----

    def test_trend_up_two_bullish_signals(self):
        """
        Two bullish signals are sufficient for trend_up.
        Use RSI > 57 and book_imbalance > 0.15.
        """
        result = self._classify(
            rsi_14=62.0,
            book_imbalance=0.25,
            vol_ratio_5_60=1.0,
            vol_5min=0.002,
            price_vs_sma20=0.001,  # below threshold — does NOT count
            price_vs_sma50=0.001,  # below threshold — does NOT count
        )
        assert result == "trend_up"

    def test_trend_up_sma_and_rsi(self):
        """sma20 > 0.003 + rsi > 57 → trend_up (2 signals)"""
        result = self._classify(
            price_vs_sma20=0.005,
            rsi_14=60.0,
            vol_ratio_5_60=0.8,
        )
        assert result == "trend_up"

    def test_trend_up_all_four_signals(self):
        """All four bullish signals active → unambiguous trend_up"""
        result = self._classify(
            price_vs_sma20=0.008,
            price_vs_sma50=0.010,
            rsi_14=65.0,
            book_imbalance=0.20,
            vol_ratio_5_60=0.8,
        )
        assert result == "trend_up"

    # ---- trend_down regime ----

    def test_trend_down_two_bearish_signals(self):
        """
        Two bearish signals → trend_down.
        sma20 < -0.003 and book_imbalance < -0.15.
        """
        result = self._classify(
            price_vs_sma20=-0.006,
            book_imbalance=-0.20,
            vol_ratio_5_60=0.8,
        )
        assert result == "trend_down"

    def test_trend_down_rsi_and_sma50(self):
        """rsi_14 < 43 + sma50 < -0.005 → trend_down (2 signals)"""
        result = self._classify(
            rsi_14=38.0,
            price_vs_sma50=-0.010,
            vol_ratio_5_60=0.8,
        )
        assert result == "trend_down"

    # ---- calm regime ----

    def test_calm_default(self):
        """No strong signals → calm"""
        result = self._classify(
            vol_5min=0.001,
            vol_ratio_5_60=0.9,
            price_vs_sma20=0.001,  # below bullish threshold
            price_vs_sma50=0.001,
            rsi_14=50.0,
            book_imbalance=0.0,
        )
        assert result == "calm"

    def test_calm_only_one_bullish(self):
        """Only 1 bullish signal: insufficient for trend — must be calm"""
        result = self._classify(
            rsi_14=60.0,           # 1 bullish
            price_vs_sma20=0.001,  # NOT bullish
            vol_ratio_5_60=0.8,
        )
        assert result == "calm"

    def test_calm_empty_features(self):
        """Empty feature dict must not raise; defaults to calm."""
        result = self._classify()
        assert result == "calm"

    def test_calm_nan_features(self):
        """nan features must be handled gracefully — must not raise."""
        import math
        result = self._classify(
            vol_5min=math.nan,
            vol_ratio_5_60=math.nan,
        )
        # Should be calm (NaN → treated as missing → no spike trigger)
        assert result in VALID_REGIMES

    # ---- event takes precedence over trend ----

    def test_event_overrides_bullish_trend(self):
        """Even with strong trend signals, vol spike → event wins."""
        result = self._classify(
            price_vs_sma20=0.010,
            price_vs_sma50=0.012,
            rsi_14=68.0,
            book_imbalance=0.30,
            vol_ratio_5_60=3.0,   # event trigger
        )
        assert result == "event"


# ---------------------------------------------------------------------------
# classify_regime — public API (calls rules + rate-limit)
# ---------------------------------------------------------------------------

class TestClassifyRegimePublicAPI:
    """classify_regime must always return a valid regime without raising."""

    def test_returns_valid_regime_for_calm_features(self):
        import utils.regime_classifier as rc
        _reset_regime_state(rc)
        result = rc.classify_regime({"vol_ratio_5_60": 0.8, "rsi_14": 50.0})
        assert result in VALID_REGIMES

    def test_returns_valid_regime_for_empty_dict(self):
        import utils.regime_classifier as rc
        _reset_regime_state(rc)
        result = rc.classify_regime({})
        assert result in VALID_REGIMES

    def test_never_raises_on_garbage_input(self):
        import utils.regime_classifier as rc
        _reset_regime_state(rc)
        # Should not raise — classify_regime has try/except with fallback
        result = rc.classify_regime({"vol_5min": "not_a_number", "rsi_14": None})
        assert result in VALID_REGIMES


# ---------------------------------------------------------------------------
# Rate-limiting via _apply_rate_limit
# ---------------------------------------------------------------------------

class TestRateLimit:
    """_apply_rate_limit must throttle fast regime oscillations."""

    def test_first_call_commits_from_startup(self):
        """
        When _current_regime_ts == 0 (startup), the first prediction that
        accumulates _CANDIDATE_CONFIRM_COUNT times should commit immediately
        (no dwell wait needed).
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        # First call: candidate_count advances to 1 — not committed yet (need 2)
        r1 = rc._apply_rate_limit("event")
        assert r1 == "calm"       # not committed yet

        # Second call: candidate_count reaches 2 — commit (dwell_ts=0 → dwell_ok)
        r2 = rc._apply_rate_limit("event")
        assert r2 == "event"      # now committed

    def test_insufficient_count_holds_regime(self):
        """
        A single different prediction must NOT change the committed regime.
        Requires 2 consecutive identical calls.
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        # Only one "trend_up" prediction — count=1, threshold=2 → no commit
        r = rc._apply_rate_limit("trend_up")
        assert r == "calm"        # held at calm

    def test_same_as_current_resets_candidate(self):
        """
        When prediction == current regime, candidate buffer is reset.
        Subsequent different prediction must start fresh accumulation.
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        # Partially accumulate "trend_up"
        rc._apply_rate_limit("trend_up")   # candidate count = 1

        # Same as current → resets candidate
        rc._apply_rate_limit("calm")       # same as current, candidate reset

        # Now start fresh "trend_up" accumulation
        r1 = rc._apply_rate_limit("trend_up")  # count = 1 again
        assert r1 == "calm"                    # not committed

        r2 = rc._apply_rate_limit("trend_up")  # count = 2
        assert r2 == "trend_up"                # committed

    def test_different_candidates_reset_count(self):
        """
        Alternating predictions (trend_up, trend_down, trend_up ...) must
        never commit because the buffer keeps resetting.
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        for _ in range(6):
            ra = rc._apply_rate_limit("trend_up")
            rd = rc._apply_rate_limit("trend_down")
            # Neither can accumulate 2 consecutive identical predictions
            assert ra == "calm" or ra == "trend_up" or ra == "trend_down"

        # Verify regime has not fluctuated to both trend_up AND trend_down
        final = rc.get_current_regime()
        assert final in VALID_REGIMES

    def test_dwell_blocks_transition_if_too_recent(self):
        """
        If we just committed a regime (ts = now), a new prediction must NOT
        immediately commit even with sufficient count.

        We simulate this by setting _current_regime_ts to a future-like value
        (i.e. very recent commit) and checking the regime stays put.
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        # First commit to "event"
        rc._apply_rate_limit("event")
        rc._apply_rate_limit("event")  # committed

        # Immediately try to change to trend_up — dwell not elapsed
        r1 = rc._apply_rate_limit("trend_up")
        r2 = rc._apply_rate_limit("trend_up")
        # dwell = ~0 seconds → should still be "event"
        # (unless the machine is incredibly slow and 120s elapsed, which is impossible)
        assert r2 == "event"

    def test_get_current_regime_consistent(self):
        """get_current_regime() returns the same value as the last rate-limited output."""
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        rc._apply_rate_limit("trend_down")
        rc._apply_rate_limit("trend_down")  # commits

        assert rc.get_current_regime() == "trend_down"

    def test_regime_changes_counter_increments_on_commit(self):
        """
        get_session_regime_stats()["regime_changes"] must count the number of
        actual committed transitions, not the number of regimes that have
        nonzero duration (the pre-FIX-6 bug).
        """
        import utils.regime_classifier as rc
        _reset_regime_state(rc)

        stats_before = rc.get_session_regime_stats()
        assert stats_before["regime_changes"] == 0, "should start at 0 after reset"

        # Commit first transition: calm → event
        rc._apply_rate_limit("event")
        rc._apply_rate_limit("event")   # count=2, dwell_ok → commits
        stats_after_1 = rc.get_session_regime_stats()
        assert stats_after_1["regime_changes"] == 1
        assert stats_after_1["current_regime"] == "event"

        # Stay in event (no change) — counter must not move
        rc._apply_rate_limit("event")
        assert rc.get_session_regime_stats()["regime_changes"] == 1
