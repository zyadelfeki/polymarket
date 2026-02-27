"""
Volatility / Regime Classifier for dynamic risk scaling.

Classifies current market conditions into one of four regimes:
  'calm'        – low volatility, no strong trend
  'trend_up'    – rising momentum (price above SMAs, RSI > 55, positive OFI)
  'trend_down'  – falling momentum (price below SMAs, RSI < 45, negative OFI)
  'event'       – spike in local volatility (vol_ratio_5_60 >> 1) or vol_5min outlier

Architecture
------------
Primary path: rule-based classifier that works with zero training data and is
fast, transparent, and auditable.  Preferred for live trading.

Secondary path (optional): KMeans-based unsupervised classifier trained on
historical feature vectors.  Enabled by calling ``train_kmeans(...)`` offline
and writing the model to ``models/regime_kmeans.pkl``.  If the model file is
present, KMeans predictions are blended with the rule-based output (KMeans
provides a second opinion; rules retain veto power for extreme values).

Rate limiter
------------
Regime transitions are rate-limited by a minimum dwell time
(default 120 s) to prevent thrashing on single-tick noise.  The global
``_current_regime`` is updated only when the new classification has been
stable for ``_DWELL_THRESHOLD_SECONDS``.

Fail-safe
---------
If classify_regime raises or the model fails to load, the caller catches the
exception.  ``main.py`` explicitly falls back to the default risk config and
logs a WARNING — never crashes.

Public API
----------
    from utils.regime_classifier import classify_regime
    regime: str = classify_regime(features)   # e.g. 'calm'
"""

from __future__ import annotations

import math
import os
import pickle
import structlog
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

logger = structlog.get_logger(__name__)

_REPO_ROOT  = Path(__file__).resolve().parent.parent
_MODEL_PATH = _REPO_ROOT / "models" / "regime_kmeans.pkl"

# ---------------------------------------------------------------------------
# Valid regimes (must match keys in config_production.REGIME_RISK_CONFIG)
# ---------------------------------------------------------------------------
REGIMES = ("calm", "trend_up", "trend_down", "event")
_DEFAULT_REGIME = "calm"

# ---------------------------------------------------------------------------
# Rate-limiting — suppress one-tick noise
# ---------------------------------------------------------------------------
_DWELL_THRESHOLD_SECONDS: float = 120.0  # minimum time in a regime before changing
_CANDIDATE_CONFIRM_COUNT: int = 2         # number of consecutive identical predictions before committing

_state_lock = threading.Lock()
_current_regime: str = _DEFAULT_REGIME
_current_regime_ts: float = 0.0          # monotonic time when regime was last committed
_candidate_regime: str = _DEFAULT_REGIME # pending regime (must repeat before commit)
_candidate_count: int = 0
_regime_durations: Dict[str, float] = {r: 0.0 for r in REGIMES}  # cumulative seconds
_session_start_ts: float = time.monotonic()
_regime_change_count: int = 0             # counts actual committed regime transitions

# ---------------------------------------------------------------------------
# KMeans model cache
# ---------------------------------------------------------------------------
_kmeans_bundle: object = None
_kmeans_load_attempted: bool = False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def classify_regime(features: dict) -> str:
    """
    Classify current market regime from a feature dict.

    Parameters
    ----------
    features:
        Dict as returned by ``utils.regime_features.get_regime_features()``.
        Missing or NaN values are handled gracefully.

    Returns
    -------
    One of: 'calm', 'trend_up', 'trend_down', 'event'

    Rate-limited: returns the *current* committed regime until the new
    prediction has been stable for ``_DWELL_THRESHOLD_SECONDS`` or repeated
    ``_CANDIDATE_CONFIRM_COUNT`` times consecutively.
    """
    try:
        raw = _rule_based_classify(features)
        # Blend with KMeans if available (secondary opinion only)
        kmeans_label = _kmeans_classify(features)
        prediction = _blend(raw, kmeans_label)
    except Exception as exc:
        logger.warning(
            "regime_classifier_error",
            error=str(exc),
            fallback=_DEFAULT_REGIME,
        )
        return _current_regime  # keep last known safe value

    return _apply_rate_limit(prediction)


def get_current_regime() -> str:
    """Return the last committed regime without computing a new one."""
    with _state_lock:
        return _current_regime


def get_session_regime_stats() -> Dict:
    """
    Return a snapshot of time-in-regime stats for the current session,
    suitable for check_session.py output.
    """
    with _state_lock:
        now_ts = time.monotonic()
        # Accumulate elapsed time in the current regime up to now
        durations = dict(_regime_durations)
        if _current_regime_ts > 0:
            durations[_current_regime] = (
                durations.get(_current_regime, 0.0)
                + (now_ts - _current_regime_ts)
            )
        total = max(sum(durations.values()), 1e-9)
        pcts = {r: round(100.0 * s / total, 1) for r, s in durations.items() if s > 0}
        return {
            "current_regime": _current_regime,
            "time_in_regime_pct": pcts,
            "regime_changes": _regime_change_count,
        }


# ---------------------------------------------------------------------------
# Offline training
# ---------------------------------------------------------------------------


def train_kmeans(
    feature_matrix: np.ndarray,
    feature_names: list,
    n_clusters: int = 4,
    model_path: str = str(_MODEL_PATH),
) -> None:
    """
    Train a KMeans model on a (n_samples, n_features) matrix and write to disk.

    Called offline, e.g.:
        from utils.regime_features import get_regime_features
        # collect hundreds of snapshots across different sessions, then:
        from utils.regime_classifier import train_kmeans
        train_kmeans(X, feature_names)

    The cluster → regime label mapping is assigned heuristically post-training
    based on the cluster centroid's vol / trend features:
      - highest vol_5min → 'event'
      - positive price_vs_sma20 + low vol → 'trend_up'
      - negative price_vs_sma20 + low vol → 'trend_down'
      - default → 'calm'
    """
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler

    # Replace NaNs with column means
    col_means = np.nanmean(feature_matrix, axis=0)
    for col_idx in range(feature_matrix.shape[1]):
        nan_mask = np.isnan(feature_matrix[:, col_idx])
        feature_matrix[nan_mask, col_idx] = col_means[col_idx]

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(feature_matrix)

    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    km.fit(X_scaled)

    # Assign labels based on centroids (in original feature space)
    centroids_orig = scaler.inverse_transform(km.cluster_centers_)
    feat_idx = {n: i for i, n in enumerate(feature_names)}

    cluster_labels: Dict[int, str] = {}
    vol_idx  = feat_idx.get("vol_5min", 0)
    sma20_idx = feat_idx.get("price_vs_sma20", 1)

    # Sort clusters by vol level
    vols = [(centroids_orig[c, vol_idx], c) for c in range(n_clusters)]
    vols.sort(reverse=True)
    event_cluster = vols[0][1]
    cluster_labels[event_cluster] = "event"

    remaining = [c for _, c in vols[1:]]
    for c in remaining:
        sma20_val = centroids_orig[c, sma20_idx]
        if sma20_val > 0.005:
            cluster_labels[c] = "trend_up"
        elif sma20_val < -0.005:
            cluster_labels[c] = "trend_down"
        else:
            cluster_labels.setdefault(c, "calm")

    for c in range(n_clusters):
        cluster_labels.setdefault(c, "calm")

    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "model":          km,
        "scaler":         scaler,
        "feature_names":  feature_names,
        "cluster_labels": cluster_labels,
        "n_clusters":     n_clusters,
    }
    tmp = model_path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(bundle, fh, protocol=5)
    os.replace(tmp, model_path)
    logger.info("regime_kmeans_model_persisted", path=model_path, n_clusters=n_clusters)


# ---------------------------------------------------------------------------
# Internal rule-based classifier
# ---------------------------------------------------------------------------


def _rule_based_classify(features: dict) -> str:
    """
    Deterministic regime classification based on hard thresholds.

    Precedence (highest wins):
      1. event   — local vol spike
      2. trend_up / trend_down — directional momentum
      3. calm    — default
    """
    vol_5      = _f(features, "vol_5min",     math.nan)
    vol_60     = _f(features, "vol_60min",    math.nan)
    vol_ratio  = _f(features, "vol_ratio_5_60", math.nan)
    sma20      = _f(features, "price_vs_sma20", 0.0)
    sma50      = _f(features, "price_vs_sma50", 0.0)
    rsi        = _f(features, "rsi_14",        50.0)
    book_imbal = _f(features, "book_imbalance", 0.0)

    # --- event: local volatility spike (vol_5 >> vol_60 or absolute spike) -
    if (
        (not math.isnan(vol_ratio) and vol_ratio >= 2.0) or
        (not math.isnan(vol_5)     and vol_5 >= 0.008)   # ~0.8% per 1-min candle
    ):
        return "event"

    # --- directional trend ---
    bullish_signals = 0
    bearish_signals = 0

    if not math.isnan(sma20):
        if sma20 > 0.003:
            bullish_signals += 1
        elif sma20 < -0.003:
            bearish_signals += 1

    if not math.isnan(sma50):
        if sma50 > 0.005:
            bullish_signals += 1
        elif sma50 < -0.005:
            bearish_signals += 1

    if not math.isnan(rsi):
        if rsi > 57:
            bullish_signals += 1
        elif rsi < 43:
            bearish_signals += 1

    if not math.isnan(book_imbal):
        if book_imbal > 0.15:
            bullish_signals += 1
        elif book_imbal < -0.15:
            bearish_signals += 1

    if bullish_signals >= 2 and bullish_signals > bearish_signals:
        return "trend_up"
    if bearish_signals >= 2 and bearish_signals > bullish_signals:
        return "trend_down"

    return "calm"


def _kmeans_classify(features: dict) -> Optional[str]:
    """Return KMeans regime or None if model unavailable."""
    bundle = _get_kmeans_model()
    if bundle is None:
        return None
    try:
        model       = bundle["model"]
        scaler      = bundle["scaler"]
        feat_names  = bundle["feature_names"]
        cluster_lbl = bundle["cluster_labels"]

        row = np.array(
            [float(features.get(n, 0.0)) for n in feat_names],
            dtype=np.float32
        ).reshape(1, -1)
        # Replace NaN with 0 (scaler mean)
        row = np.nan_to_num(row, nan=0.0)

        X_scaled = scaler.transform(row)
        cluster_id = int(model.predict(X_scaled)[0])
        return cluster_lbl.get(cluster_id, "calm")
    except Exception as exc:
        logger.warning("regime_kmeans_inference_error", error=str(exc))
        return None


def _blend(rule_regime: str, kmeans_regime: Optional[str]) -> str:
    """
    Merge rule-based and KMeans predictions.  Rules have veto power for
    'event' (high-vol spike detection is safety-critical).
    """
    if kmeans_regime is None or rule_regime == "event":
        return rule_regime
    # If both agree → definitive
    if rule_regime == kmeans_regime:
        return rule_regime
    # Disagreement: rule-based is more conservative; keep its output
    # unless KMeans has stronger evidence of a trend.
    if rule_regime == "calm" and kmeans_regime in ("trend_up", "trend_down"):
        return kmeans_regime  # KMeans sees trend we missed
    return rule_regime


def _apply_rate_limit(prediction: str) -> str:
    """
    Suppress regime changes that haven't been stable long enough.

    - Prediction must repeat ``_CANDIDATE_CONFIRM_COUNT`` times consecutively
      (each call from the periodic 60-second task counts as one).
    - Additionally, the current regime must have been held for at least
      ``_DWELL_THRESHOLD_SECONDS`` before any change is allowed.
    """
    global _current_regime, _current_regime_ts
    global _candidate_regime, _candidate_count

    now_ts = time.monotonic()

    with _state_lock:
        if prediction == _current_regime:
            # Same as current — reset candidate buffer and return immediately
            _candidate_regime = prediction
            _candidate_count  = 0
            return _current_regime

        # Different from current — accumulate in candidate buffer
        if prediction == _candidate_regime:
            _candidate_count += 1
        else:
            _candidate_regime = prediction
            _candidate_count  = 1

        # Check dwell time before committing
        dwell_elapsed = now_ts - _current_regime_ts
        dwell_ok = (_current_regime_ts == 0.0) or (dwell_elapsed >= _DWELL_THRESHOLD_SECONDS)
        count_ok = (_candidate_count >= _CANDIDATE_CONFIRM_COUNT)

        if dwell_ok and count_ok:
            # Commit regime change
            global _regime_change_count
            old_regime = _current_regime
            _regime_durations[old_regime] = (
                _regime_durations.get(old_regime, 0.0) + dwell_elapsed
            )
            _current_regime    = prediction
            _current_regime_ts = now_ts
            _candidate_count   = 0
            _regime_change_count += 1
            logger.info(
                "regime_changed",
                old_regime=old_regime,
                new_regime=prediction,
                dwell_elapsed_s=round(dwell_elapsed, 1),
            )
            return prediction

        return _current_regime


def _get_kmeans_model():
    global _kmeans_bundle, _kmeans_load_attempted
    if _kmeans_load_attempted:
        return _kmeans_bundle if _kmeans_bundle is not False else None
    _kmeans_load_attempted = True
    if not _MODEL_PATH.exists():
        _kmeans_bundle = False
        return None
    try:
        with open(_MODEL_PATH, "rb") as fh:
            _kmeans_bundle = pickle.load(fh)
        logger.info("regime_kmeans_model_loaded", path=str(_MODEL_PATH))
        return _kmeans_bundle
    except Exception as exc:
        logger.warning("regime_kmeans_model_load_failed", error=str(exc))
        _kmeans_bundle = False
        return None


def _f(d: dict, key: str, default: float) -> float:
    v = d.get(key, default)
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else default
    except (TypeError, ValueError):
        return default
