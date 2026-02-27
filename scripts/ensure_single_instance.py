"""
Single-instance guard for the Polymarket bot.

On startup, writes the current PID to run/bot.pid.
If a PID file already exists and that process is still alive (psutil check),
exits immediately to prevent two instances from sharing the same in-memory
cooldown state and double-firing orders.

Windows-compatible (uses PID file + psutil instead of fcntl file locks,
which are Unix-only).
"""
import sys
import os
import atexit

_LOCK_PATH = os.path.join(os.path.dirname(__file__), '..', 'run', 'bot.pid')


def acquire_instance_lock() -> None:
    """
    Call once, at the very start of main(), before any bot state is
    initialised.  Exits with code 1 if another instance is already running.
    """
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)

    if os.path.exists(_LOCK_PATH):
        try:
            pid = int(open(_LOCK_PATH).read().strip())
            import psutil
            if psutil.pid_exists(pid):
                print(
                    f"ERROR: Bot already running as PID {pid}. "
                    "Exiting to prevent duplicate orders.",
                    flush=True,
                )
                sys.exit(1)
        except Exception:
            # Stale / corrupt PID file — safe to overwrite.
            pass

    with open(_LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))

    # Remove the PID file when the process exits cleanly or via atexit.
    atexit.register(
        lambda: os.unlink(_LOCK_PATH) if os.path.exists(_LOCK_PATH) else None
    )
