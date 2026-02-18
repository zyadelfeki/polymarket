#!/usr/bin/env python3
import argparse
import subprocess
import sys
import time
from pathlib import Path


def count_occurrences(content: str, token: str) -> int:
    return content.count(token)


def safe_div(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def run_validation(minutes: int, db_path: str, log_path: str, min_execution_rate: float, mode: str) -> int:
    log_file = Path(log_path)
    if log_file.exists():
        log_file.unlink()

    cmd = [
        sys.executable,
        "run_production_bot.py",
        "--mode",
        mode,
        "--db-path",
        db_path,
    ]

    print(f"Starting bot validation run for {minutes} minute(s)...")
    proc = subprocess.Popen(cmd)
    duration_seconds = max(1, minutes * 60)

    try:
        time.sleep(duration_seconds)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    if not log_file.exists():
        print(f"FAIL: log file not found: {log_file}")
        return 2

    content = log_file.read_text(encoding="utf-8", errors="ignore")

    opportunities = count_occurrences(content, "opportunity_found")
    executions = count_occurrences(content, "Trade executed") + count_occurrences(content, "order_filled")
    balance_syncs = count_occurrences(content, "balance_synced")
    growth_mode_hits = count_occurrences(content, "growth_mode_active")
    rounded_up_hits = count_occurrences(content, "position_rounded_up")
    fee_adjust_hits = count_occurrences(content, "edge_adjusted_for_fees")
    cb_micro_hits = count_occurrences(content, "max_drawdown_pct=50.0")

    execution_rate = safe_div(executions, opportunities)

    print("\n=== Phase 6 Validation Summary ===")
    print(f"Opportunities: {opportunities}")
    print(f"Executions: {executions}")
    print(f"Execution rate: {execution_rate:.1f}%")
    print(f"Balance sync events: {balance_syncs}")
    print(f"Growth mode events: {growth_mode_hits}")
    print(f"Rounded-up events: {rounded_up_hits}")
    print(f"Fee-adjust events: {fee_adjust_hits}")
    print(f"Micro-cap circuit-breaker events: {cb_micro_hits}")

    checks = {
        "execution_rate": execution_rate >= min_execution_rate,
        "balance_sync": balance_syncs > 0,
        "growth_mode": growth_mode_hits > 0,
        "fee_adjustment": fee_adjust_hits > 0,
        "circuit_breaker_micro": cb_micro_hits > 0,
    }

    print("\n=== Checklist ===")
    for key, passed in checks.items():
        status = "PASS" if passed else "FAIL"
        print(f"{key}: {status}")

    if all(checks.values()):
        print("\nOverall: PASS")
        return 0

    print("\nOverall: FAIL")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6 5-minute validation runner")
    parser.add_argument("--minutes", type=int, default=5)
    parser.add_argument("--mode", choices=["paper", "live"], default="paper")
    parser.add_argument("--db-path", default="data/phase6_validation.db")
    parser.add_argument("--log-path", default="bot_production.log")
    parser.add_argument("--min-execution-rate", type=float, default=30.0)
    args = parser.parse_args()

    return run_validation(
        minutes=args.minutes,
        db_path=args.db_path,
        log_path=args.log_path,
        min_execution_rate=args.min_execution_rate,
        mode=args.mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
