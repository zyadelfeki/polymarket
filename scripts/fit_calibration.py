#!/usr/bin/env python3
"""
Fit Platt scaler from calibration dataset.

Guard: exits without fitting if sample count < 100.
When sufficient data is available, fits LogisticRegression(C=1.0) on
logit(p_win_raw) → actual_outcome and saves to models/platt_scaler.pkl.

Prints ECE before/after and a 10-bin reliability table.
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import numpy as np

CSV_PATH = Path("data/calibration_dataset.csv")
SCALER_PATH = Path("models/platt_scaler.pkl")
MIN_SAMPLES = 100


def _compute_ece(
    p_wins: np.ndarray, actuals: np.ndarray, n_bins: int = 10
) -> float:
    """Expected Calibration Error (10-bin)."""
    ece = 0.0
    total = len(p_wins)
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        mask = (p_wins >= lo) & (p_wins < hi) if i < n_bins - 1 else (p_wins >= lo)
        if mask.sum() == 0:
            continue
        mean_conf = p_wins[mask].mean()
        mean_acc = actuals[mask].mean()
        ece += (mask.sum() / total) * abs(mean_conf - mean_acc)
    return ece


def _reliability_table(
    p_wins: np.ndarray, actuals: np.ndarray, n_bins: int = 10
) -> list[tuple[float, float, int]]:
    """Return [(mean_pred, actual_rate, count)] per bin."""
    table = []
    for i in range(n_bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        mask = (p_wins >= lo) & (p_wins < hi) if i < n_bins - 1 else (p_wins >= lo)
        cnt = int(mask.sum())
        if cnt == 0:
            table.append((lo + 0.05, float("nan"), 0))
        else:
            table.append((float(p_wins[mask].mean()), float(actuals[mask].mean()), cnt))
    return table


def main():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found. Run build_calibration_dataset.py first.")
        sys.exit(1)

    # Load data
    p_raw, actual = [], []
    with open(CSV_PATH, "r", newline="") as f:
        for row in csv.DictReader(f):
            p_raw.append(float(row["p_win_raw"]))
            actual.append(1 if float(row["actual_outcome"]) >= 0.5 else 0)

    n = len(p_raw)
    print(f"Calibration samples: {n}")

    if n < MIN_SAMPLES:
        print(
            f"\n⚠  WARNING: Only {n} samples available (minimum {MIN_SAMPLES} required).\n"
            f"   Platt scaling with {n} points would overfit.\n"
            f"   Collect more data — at ~20-30 settlements/day this needs {(MIN_SAMPLES - n) // 25 + 1} more days.\n"
            f"   Exiting without fitting."
        )
        # Still print uncalibrated stats
        p_arr = np.array(p_raw)
        a_arr = np.array(actual)
        ece = _compute_ece(p_arr, a_arr)
        print(f"\nUncalibrated ECE: {ece:.4f}")
        print(f"Avg p_win_raw: {p_arr.mean():.4f}  |  Actual win rate: {a_arr.mean():.4f}")
        sys.exit(0)

    p_arr = np.array(p_raw)
    a_arr = np.array(actual)

    # ECE before
    ece_before = _compute_ece(p_arr, a_arr)
    print(f"\nECE BEFORE calibration: {ece_before:.4f}")

    # Fit logistic regression on logit(p_win) → actual
    from sklearn.linear_model import LogisticRegression
    import joblib

    logits = np.log(np.clip(p_arr, 1e-6, 1 - 1e-6) / (1 - np.clip(p_arr, 1e-6, 1 - 1e-6)))
    X = logits.reshape(-1, 1)
    model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
    model.fit(X, a_arr)

    # Calibrated predictions
    p_cal = model.predict_proba(X)[:, 1]
    ece_after = _compute_ece(p_cal, a_arr)
    print(f"ECE AFTER  calibration: {ece_after:.4f}")

    if ece_after >= ece_before:
        print("\n⚠  Calibration did NOT improve ECE. Scaler NOT saved.")
        print("   This may indicate the model is already well-calibrated or data is too noisy.")
    else:
        SCALER_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, str(SCALER_PATH))
        print(f"\n✓  Scaler saved to {SCALER_PATH}")
        print(f"   ECE improvement: {ece_before:.4f} → {ece_after:.4f}")

    # Reliability table
    print("\n--- Reliability Table (10 bins) ---")
    print(f"  {'Bin':>8s}  {'Mean Pred':>10s}  {'Actual Rate':>12s}  {'Count':>6s}")
    table = _reliability_table(p_arr if ece_after >= ece_before else p_cal, a_arr)
    for i, (mp, ar, cnt) in enumerate(table):
        lo = i / 10
        hi = (i + 1) / 10
        ar_str = f"{ar:.3f}" if cnt > 0 else "  N/A"
        print(f"  [{lo:.1f}-{hi:.1f})  {mp:10.4f}  {ar_str:>12s}  {cnt:6d}")


if __name__ == "__main__":
    main()
