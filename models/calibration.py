"""
Platt scaling calibration for Charlie p_win probabilities.

Two modes:
  - If ``models/platt_scaler.pkl`` exists → apply logistic calibration
  - Otherwise → passthrough (return p_win unchanged)

The scaler is fitted offline by ``scripts/fit_calibration.py`` once
``data/calibration_dataset.csv`` accumulates ≥ 100 samples.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_SCALER_PATH = Path(__file__).resolve().parent / "platt_scaler.pkl"

_scaler = None
_scaler_loaded = False  # distinguish "not yet tried" from "tried but missing"


def _load_scaler():
    """Try to load the scaler once.  Returns the model or None."""
    global _scaler, _scaler_loaded
    if _scaler_loaded:
        return _scaler
    _scaler_loaded = True
    if not _SCALER_PATH.exists():
        logger.info(
            "platt_scaler_not_found — calibration in passthrough mode (path=%s)",
            _SCALER_PATH,
        )
        return None
    try:
        import joblib
        _scaler = joblib.load(str(_SCALER_PATH))
        logger.info("platt_scaler_loaded from %s", _SCALER_PATH)
        return _scaler
    except Exception as exc:
        logger.warning("platt_scaler_load_failed: %s", exc)
        return None


def calibrate_p_win(p_win_raw: float) -> float:
    """Return calibrated p_win.  Passthrough if no scaler is available."""
    model = _load_scaler()
    if model is None:
        return p_win_raw

    # Logit transform — clamp to avoid ±inf
    p_clamped = max(1e-6, min(1.0 - 1e-6, p_win_raw))
    logit = np.log(p_clamped / (1.0 - p_clamped))
    calibrated = float(model.predict_proba([[logit]])[0][1])
    return calibrated
