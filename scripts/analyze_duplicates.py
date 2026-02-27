"""
Part C: Consolidate v1 vs v2 file pairs.
Run from polymarket directory.
"""
import ast
import os
from pathlib import Path


def get_public_names(path: str) -> set:
    """Return set of top-level public function/class names in the file."""
    try:
        tree = ast.parse(Path(path).read_text(errors="replace"))
        return {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and not n.name.startswith("_")
        }
    except Exception as e:
        print(f"  ERROR parsing {path}: {e}")
        return set()


pairs = [
    ("services/execution_service.py",       "services/execution_service_v2.py"),
    ("services/health_monitor.py",          "services/health_monitor_v2.py"),
    ("data_feeds/polymarket_client.py",     "data_feeds/polymarket_client_v2.py"),
    ("data_feeds/binance_websocket.py",     "data_feeds/binance_websocket_v2.py"),
    ("risk/circuit_breaker.py",             "risk/circuit_breaker_v2.py"),
]

for v1_path, v2_path in pairs:
    print(f"\n{'='*60}")
    print(f"V1: {v1_path}")
    print(f"V2: {v2_path}")
    v1_exists = Path(v1_path).exists()
    v2_exists = Path(v2_path).exists()
    print(f"  V1 exists: {v1_exists}  V2 exists: {v2_exists}")
    if not v1_exists or not v2_exists:
        continue

    v1_names = get_public_names(v1_path)
    v2_names = get_public_names(v2_path)
    only_in_v1 = v1_names - v2_names
    print(f"  V1 public names ({len(v1_names)}): {sorted(v1_names)[:5]}{'...' if len(v1_names)>5 else ''}")
    print(f"  V2 public names ({len(v2_names)}): {sorted(v2_names)[:5]}{'...' if len(v2_names)>5 else ''}")
    if only_in_v1:
        print(f"  *** ONLY IN V1 (need to port or check): {sorted(only_in_v1)}")
    else:
        print(f"  V2 is a superset of V1 — safe to consolidate")


print("\n" + "="*60)
print("STRATEGY DIRECTORIES")
print("strategy/ files:", [f.name for f in Path("strategy").iterdir() if f.suffix == ".py"] if Path("strategy").exists() else "NOT FOUND")
print("strategies/ files:", [f.name for f in Path("strategies").iterdir() if f.suffix == ".py"] if Path("strategies").exists() else "NOT FOUND")

print("\nBACKTEST DIRECTORIES")
print("backtest/ files:", [f.name for f in Path("backtest").iterdir() if f.suffix == ".py"] if Path("backtest").exists() else "NOT FOUND")
print("backtesting/ files:", [f.name for f in Path("backtesting").iterdir() if f.suffix == ".py"] if Path("backtesting").exists() else "NOT FOUND")
