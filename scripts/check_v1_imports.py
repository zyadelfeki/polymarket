"""Check what imports v1 files across the project."""
import os
from pathlib import Path

v1_modules = [
    ("services.execution_service",   "ExecutionService"),
    ("services.health_monitor",      "HealthMonitor"),
    ("data_feeds.polymarket_client", "PolymarketClient"),
    ("data_feeds.binance_websocket", "BinanceWebSocketFeed"),
    ("risk.circuit_breaker",         "CircuitBreaker"),
]

root = Path(".")
py_files = list(root.rglob("*.py"))
# Exclude __pycache__, .venv, tests
py_files = [
    f for f in py_files
    if "__pycache__" not in str(f)
    and ".venv" not in str(f)
    and "venv" not in str(f)
    and "scripts" not in str(f)
]

for module, class_name in v1_modules:
    refs = []
    for f in py_files:
        try:
            src = f.read_text(errors="replace")
            if module in src or class_name in src:
                refs.append(str(f))
        except Exception:
            pass
    if refs:
        print(f"\n{module} / {class_name}: imported in {len(refs)} files")
        for r in refs[:8]:
            print(f"  {r}")
    else:
        print(f"\n{module} / {class_name}: NO IMPORTS — safe to delete v1")
