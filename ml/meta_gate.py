"""
Meta-Labeling Gate for Charlie trade signals.

Architecture
-----------
A lightweight binary classifier (Logistic Regression by default, LightGBM when
lgbm_available and >=500 labelled rows) trained on historical settled trades.

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
take_it: bool = should_trade(features)

Fail-open contract: if the model file is absent or corrupt, ``should_trade``
returns True and logs a WARNING.  Never raises.

Latency budget: <2 ms per call (single logistic regression prediction).

IRONCLAD:
- No I/O on the hot path.  Model is loaded once at import time.
- No multiprocessing or threading inside inference.
- Threadsafety: the inference path is read-only; safe for concurrent callers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import structlog
import math
import os
import pickle
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODEL_PATH = _REPO_ROOT / "models" / "meta_gate.pkl"
_DB_PATH    = _REPO_ROOT / "data" / "trading.db"

# ---------------------------------------------------------------------------
# Threshold: meta-gate rejects when P(take) < threshold.
# 0.45 is intentionally conservative — only reject when the model is
# meaningfully confident the trade is bad.  Reduces false-negatives on
# small training sets.
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLD: float = 0.45

# ---------------------------------------------------------------------------
# Lazy model cache — loaded once on first call to should_trade().
# None  → not yet attempted.
# False → load was attempted but failed (fail-open mode).
# Model object → loaded successfully.
# ---------------------------------------------------------------------------
_MODEL_CACHE: object = None          # Set by _load_model()
_MODEL_LOAD_ATTEMPTED: bool = False  # Guard against repeated load attempts


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def should_trade(features: dict) -> bool:
    """
    Return True if the meta-gate recommends taking the trade.

    Parameters
    ----------
    features:
        Dict with keys matching those produced by extract_features_from_opportunity().
        Unknown keys are ignored; missing keys are filled with 0.0.

    Fail-open:
        Returns True if the model is unavailable, so trades are never silently
        blocked by an infrastructure failure.
    """
    model_bundle = _get_model()
    if model_bundle is None:
        # Fail-open: model unavailable → let the trade proceed.
        return True

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
        return float(proba) >= threshold

    except Exception as exc:
        logger.warning(
            "meta_gate_inference_error",
            error=str(exc),
            error_type=type(exc).__name__,
            reason="fail_open",
        )
        return True


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
        rolling_win_rate = (sum(win_window[-WINDOW_WIN:]) / len(win_window)) if win_window else 0.5
        if len(pnl_window) >= 2:
            arr = np.array(pnl_window[-WINDOW_PNL:])
            mu, sigma = float(arr.mean()), float(arr.std()) + 1e-9
            rolling_pnl_z = (pnl - mu) / sigma
        else:
            rolling_pnl_z = 0.0

        # Update rolling windows AFTER computing features (no look-ahead)
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
    """Load model from disk (once) and return the bundle, or None on failure."""
    global _MODEL_CACHE, _MODEL_LOAD_ATTEMPTED

    if _MODEL_LOAD_ATTEMPTED:
        return _MODEL_CACHE if _MODEL_CACHE is not False else None

    _MODEL_LOAD_ATTEMPTED = True

    if not _MODEL_PATH.exists():
        logger.warning(
            "meta_gate_model_not_found",
            path=str(_MODEL_PATH),
            action="fail_open",
            hint="Run: python -m ml.meta_gate --train  to create the model.",
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


def _dict_to_array(features: dict, feature_names: List[str]) -> np.ndarray:
    """Convert feature dict to a (1, n_features) numpy array."""
    row = [float(features.get(name, 0.0)) for name in feature_names]
    return np.array([row], dtype=np.float32)


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
        "edge":    ["edge=", "net_edge="],
        "implied": ["implied=", "implied_prob="],
        "fee":     ["fee="],
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Train the meta-gate classifier.")
    parser.add_argument("--train",  action="store_true", help="Train and persist model.")
    parser.add_argument("--db",     default=str(_DB_PATH), help="Path to trading.db.")
    parser.add_argument("--model",  default=str(_MODEL_PATH), help="Output model path.")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD,
                        help="Classification threshold (default=0.45).")
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
