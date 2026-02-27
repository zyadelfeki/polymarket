#!/usr/bin/env python3
"""
Build calibration dataset from production log.

Parses ``logs/production.log`` to extract:
  - ``order_submission_attempt`` events with ``charlie_p_win``
  - ``order_settled_live`` events with ``pnl`` (positive = WIN)

Matches by ``market_id``, writes to ``data/calibration_dataset.csv``
(append-only: new runs add rows for any markets not already present).

Prints uncalibrated ECE (10-bin Expected Calibration Error).
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

LOG_PATH = Path("logs/production.log")
CSV_PATH = Path("data/calibration_dataset.csv")


def _parse_log() -> tuple[dict, dict]:
    """Return (submissions, settlements) dicts keyed by market_id."""
    submissions: dict[str, dict] = {}
    settlements: dict[str, float] = {}

    remainder = ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        while True:
            chunk = f.read(10 * 1024 * 1024)
            if not chunk:
                break
            data = remainder + chunk
            lines = data.split("\n")
            remainder = lines[-1]

            for line in lines[:-1]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if not isinstance(obj, dict):
                        continue
                except (json.JSONDecodeError, ValueError):
                    continue

                event = obj.get("event", "")

                if (
                    event == "order_submission_attempt"
                    and obj.get("charlie_p_win") is not None
                ):
                    mid = obj.get("market_id")
                    if mid and mid not in submissions:
                        submissions[mid] = {
                            "p_win_raw": float(obj["charlie_p_win"]),
                        }

                if event == "order_settled_live":
                    mid = obj.get("market_id")
                    pnl = obj.get("pnl")
                    if mid and pnl is not None:
                        pnl_val = float(pnl)
                        settlements[mid] = settlements.get(mid, 0.0) + pnl_val

    return submissions, settlements


def _compute_ece(p_wins: list[float], actuals: list[int], n_bins: int = 10) -> float:
    """Expected Calibration Error (10-bin)."""
    if not p_wins:
        return 0.0
    bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]
    for p, a in zip(p_wins, actuals):
        idx = min(int(p * n_bins), n_bins - 1)
        bins[idx].append((p, a))

    total = len(p_wins)
    ece = 0.0
    for b in bins:
        if not b:
            continue
        mean_conf = sum(x[0] for x in b) / len(b)
        mean_acc = sum(x[1] for x in b) / len(b)
        ece += (len(b) / total) * abs(mean_conf - mean_acc)
    return ece


def main():
    if not LOG_PATH.exists():
        print(f"ERROR: {LOG_PATH} not found")
        sys.exit(1)

    submissions, settlements = _parse_log()

    # Load existing CSV to avoid duplicates
    existing_markets: set[str] = set()
    if CSV_PATH.exists():
        with open(CSV_PATH, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_markets.add(row.get("market_id", ""))

    # Match and collect new rows
    new_rows = []
    for mid, sub in submissions.items():
        if mid in settlements and mid not in existing_markets:
            actual = 1 if settlements[mid] > 0 else 0
            new_rows.append(
                {
                    "market_id": mid,
                    "p_win_raw": round(sub["p_win_raw"], 6),
                    "actual_outcome": actual,
                }
            )

    # Append to CSV
    write_header = not CSV_PATH.exists()
    if new_rows:
        with open(CSV_PATH, "a", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["market_id", "p_win_raw", "actual_outcome"]
            )
            if write_header:
                writer.writeheader()
            writer.writerows(new_rows)

    # Read full CSV for stats
    all_p, all_a = [], []
    if CSV_PATH.exists():
        with open(CSV_PATH, "r", newline="") as f:
            for row in csv.DictReader(f):
                all_p.append(float(row["p_win_raw"]))
                all_a.append(int(row["actual_outcome"]))

    print(f"New rows appended: {len(new_rows)}")
    print(f"Total calibration samples: {len(all_p)}")

    if all_p:
        wins = sum(all_a)
        avg_p = sum(all_p) / len(all_p)
        actual_rate = wins / len(all_p)
        ece = _compute_ece(all_p, all_a)
        print(f"Avg p_win_raw: {avg_p:.4f}")
        print(f"Actual win rate: {actual_rate:.4f}")
        print(f"Uncalibrated ECE: {ece:.4f}")
    else:
        print("No samples yet — waiting for trades to settle.")


if __name__ == "__main__":
    main()
