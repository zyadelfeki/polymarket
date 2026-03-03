#!/usr/bin/env python3
"""
Manually reset the circuit breaker for the LIVE bot.

Usage (with venv active):
    python scripts/reset_circuit_breaker.py

Only needed in LIVE mode.  Paper mode auto-resets the CB on every startup
(see the 'paper_circuit_breaker_reset' log line in main.py).

The CB is an in-memory object inside the running bot process — this script
cannot reach it directly.  Instead it writes a sentinel file:

    runtime/cb_reset.flag

main.py's _periodic_check reads this file, calls circuit_breaker.manual_reset(),
then deletes the flag.  The reset takes effect on the next periodic_check cycle
(≤ loop_tick_seconds, typically ≤ 10 s).

IMPORTANT: This script does NOT stop or restart the bot.  The running process
handles the reset asynchronously once it sees the flag file.
"""

import sys
from pathlib import Path

FLAG_PATH = Path("runtime/cb_reset.flag")


def main() -> None:
    # Validate we're at the repo root (or close to it).
    if not Path("main.py").exists():
        print(
            "ERROR: Run this script from the polymarket repo root.\n"
            "  cd /path/to/polymarket && python scripts/reset_circuit_breaker.py",
            file=sys.stderr,
        )
        sys.exit(1)

    FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if FLAG_PATH.exists():
        print("Flag already present — the bot hasn't consumed it yet.")
        print(f"  Path: {FLAG_PATH.resolve()}")
        print("Wait for the next periodic_check cycle (~10 s) and try again if stuck.")
        return

    FLAG_PATH.write_text("reset\n")
    print(f"Reset flag written to: {FLAG_PATH.resolve()}")
    print("The running bot will reset the circuit breaker on its next tick (≤ 10 s).")
    print("Watch for 'circuit_breaker_manual_reset' in the log.")


if __name__ == "__main__":
    main()
