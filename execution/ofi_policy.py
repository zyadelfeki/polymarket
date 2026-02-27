"""
OFI-Driven Execution Policy.

Learns how OFI, spread, and liquidity relate to optimal aggressiveness and
chooses an execution action from a small discrete action space.

Action space
------------
  0 – STANDARD:  current default behaviour (unchanged)
  1 – AGGRESSIVE: larger size slice, earlier in the window
  2 – PASSIVE:   split size, submit closer to expiry

IRONCLAD: This module starts in LOGGING-ONLY mode.  The ``choose_execution_action``
function always returns 0 (STANDARD) until ``LIVE_MODE = True`` is explicitly set
by the operator after offline validation.  The chosen action and features are
logged as ``ofi_execution_action`` events so the distribution can be monitored
in check_session.py without any live behaviour change.

Policy learning (offline)
-------------------------
Collect ``ofi_execution_action`` log events over several sessions.  For each
event, label which action would have produced the best outcome:
  - action 1 (AGGRESSIVE) is better when |OFI| is high AND spread is tight
    AND the fill was chased (slippage > median)
  - action 2 (PASSIVE) is better when |OFI| is low AND fill was immediate
  - action 0 (STANDARD) is the baseline

Train a LogisticRegression / RandomForest mapping (ofi_z, spread, depth) →
action label, then enable live mode after offline validation.

Inference latency: single sklearn predict_proba call, < 1 ms.

Public API
----------
    from execution.ofi_policy import choose_execution_action, log_ofi_action

    action, feats = choose_execution_action(ofi_features)
    log_ofi_action(logger_handle, market_id, action, feats)
"""

from __future__ import annotations

import math
import os
import pickle
import structlog
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = structlog.get_logger(__name__)

_REPO_ROOT  = Path(__file__).resolve().parent.parent
_MODEL_PATH = _REPO_ROOT / "models" / "ofi_policy.pkl"

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# LIVE_MODE graduation criteria (set to True when ALL are met):
# 1. >= 500 ofi_execution_action log events accumulated across sessions.
# 2. Offline analysis shows OFI-confirmed trades win_rate > OFI-conflict trades
#    win_rate by >= 3 percentage points (run scripts/ofi_analysis.py).
# 3. Manual review of analysis by operator.
# Until then, all OFI decisions are LOG-ONLY (no order modification).
# ---------------------------------------------------------------------------
LIVE_MODE: bool = False

# Action labels for logging
ACTION_LABELS = {
    0: "standard",
    1: "aggressive",
    2: "passive",
}

_model_cache: object = None
_model_load_attempted: bool = False


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def choose_execution_action(features: dict) -> Tuple[int, dict]:
    """
    Recommend an execution action based on current OFI, spread, and depth.

    Parameters
    ----------
    features:
        Dict with keys (all optional, filled with 0.0 if absent):
          ofi_z           – z-scored OFI (current / rolling stddev)
          ofi_direction   – +1 buy-dominant, -1 sell-dominant, 0 neutral
          spread_bps      – current bid-ask spread in basis points
          depth_ratio     – approx bid depth / ask depth ratio
          volatility      – recent realised volatility (from regime_features)
          time_to_expiry  – seconds until market closes (0 if unknown)

    Returns
    -------
    (action: int, features_used: dict)
        action 0 = STANDARD, 1 = AGGRESSIVE, 2 = PASSIVE.
        Always returns (0, features_used) when LIVE_MODE is False
        or when the model is unavailable.
    """
    enriched = _enrich_features(features)

    if not LIVE_MODE:
        return 0, enriched

    # Attempt model-based recommendation
    bundle = _get_model()
    if bundle is None:
        return 0, enriched

    try:
        model     = bundle["model"]
        scaler    = bundle.get("scaler")
        feat_names: list = bundle["feature_names"]

        row = np.array(
            [float(enriched.get(n, 0.0)) for n in feat_names],
            dtype=np.float32,
        ).reshape(1, -1)
        row = np.nan_to_num(row, nan=0.0)

        if scaler is not None:
            row = scaler.transform(row)

        action = int(model.predict(row)[0])
        action = max(0, min(2, action))  # guard against out-of-range
        return action, enriched

    except Exception as exc:
        logger.warning(
            "ofi_policy_inference_error",
            error=str(exc),
            fallback_action=0,
        )
        return 0, enriched


def log_ofi_action(
    log: Any,
    market_id: str,
    action: int,
    features: dict,
    *,
    realized_slippage_bps: Optional[float] = None,
) -> None:
    """
    Emit a structured ``ofi_execution_action`` log event.  Called by main.py
    immediately after ``choose_execution_action``.

    When ``realized_slippage_bps`` is supplied (post-fill), the event doubles
    as a training sample for offline policy learning.
    """
    log.info(
        "ofi_execution_action",
        market_id=market_id,
        action=action,
        action_label=ACTION_LABELS.get(action, "unknown"),
        live_mode=LIVE_MODE,
        ofi_z=round(features.get("ofi_z", 0.0), 3),
        ofi_direction=features.get("ofi_direction", 0),
        spread_bps=round(features.get("spread_bps", 0.0), 1),
        depth_ratio=round(features.get("depth_ratio", 1.0), 3),
        volatility=round(features.get("volatility", 0.0), 5),
        time_to_expiry=features.get("time_to_expiry", 0),
        realized_slippage_bps=realized_slippage_bps,
    )


def build_ofi_features(
    *,
    extra_features: Optional[dict] = None,
    spread_bps: float = 0.0,
    depth_ratio: float = 1.0,
    volatility: float = 0.0,
    time_to_expiry: float = 0.0,
) -> dict:
    """
    Construct the feature dict from data already available in main.py.

    Convenience wrapper so main.py doesn't need to know the internal
    feature names.  ``extra_features`` is the Binance feature dict from
    ``get_all_features``; OFI-related keys are extracted if present.
    """
    ef = extra_features or {}
    ofi_bids = ef.get("ofi_bids") or []
    ofi_asks = ef.get("ofi_asks") or []

    ofi_z, ofi_dir = _compute_ofi_stats(ofi_bids, ofi_asks)

    return {
        "ofi_z":          ofi_z,
        "ofi_direction":  ofi_dir,
        "spread_bps":     spread_bps,
        "depth_ratio":    depth_ratio,
        "volatility":     volatility,
        "time_to_expiry": time_to_expiry,
    }


# ---------------------------------------------------------------------------
# Offline training
# ---------------------------------------------------------------------------


def train_policy(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list,
    model_path: str = str(_MODEL_PATH),
) -> None:
    """
    Train and persist the OFI execution policy from labelled offline data.

    Parameters
    ----------
    X:             (n_samples, n_features) feature matrix
    y:             (n_samples,) integer labels (0, 1, or 2)
    feature_names: list of column names matching X columns

    Called offline after collecting enough ``ofi_execution_action`` log events
    and labelling them with the best action in hindsight.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    X_s = scaler.fit_transform(X)

    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=5,
        class_weight="balanced",
        random_state=42,
        n_jobs=1,
    )
    model.fit(X_s, y)

    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    bundle = {
        "model":         model,
        "scaler":        scaler,
        "feature_names": feature_names,
    }
    tmp = model_path + ".tmp"
    with open(tmp, "wb") as fh:
        pickle.dump(bundle, fh, protocol=5)
    os.replace(tmp, model_path)
    logger.info("ofi_policy_model_persisted", path=model_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enrich_features(features: dict) -> dict:
    """Return a copy of features with derived fields filled."""
    out = dict(features)
    out.setdefault("ofi_z", 0.0)
    out.setdefault("ofi_direction", 0)
    out.setdefault("spread_bps", 0.0)
    out.setdefault("depth_ratio", 1.0)
    out.setdefault("volatility", 0.0)
    out.setdefault("time_to_expiry", 0.0)
    return out


def _compute_ofi_stats(
    bids: list,
    asks: list,
) -> Tuple[float, int]:
    """
    Compute a simple instantaneous OFI z-score and direction from level-1 book.

    ofi_z:       (bid_vol - ask_vol) normalised by total; in [-1, 1].
    ofi_direction: +1 bid-dominant, -1 ask-dominant, 0 balanced.
    """
    try:
        bid_vol = sum(float(b[1]) for b in bids if len(b) >= 2)
        ask_vol = sum(float(a[1]) for a in asks if len(a) >= 2)
        total   = bid_vol + ask_vol
        if total < 1e-9:
            return 0.0, 0
        raw_ofi = (bid_vol - ask_vol) / total
        direction = 1 if raw_ofi > 0.05 else (-1 if raw_ofi < -0.05 else 0)
        return round(raw_ofi, 4), direction
    except Exception:
        return 0.0, 0


def _get_model():
    global _model_cache, _model_load_attempted
    if _model_load_attempted:
        return _model_cache if _model_cache is not False else None
    _model_load_attempted = True
    if not _MODEL_PATH.exists():
        _model_cache = False
        return None
    try:
        with open(_MODEL_PATH, "rb") as fh:
            _model_cache = pickle.load(fh)
        logger.info("ofi_policy_model_loaded", path=str(_MODEL_PATH))
        return _model_cache
    except Exception as exc:
        logger.warning("ofi_policy_model_load_failed", error=str(exc))
        _model_cache = False
        return None
