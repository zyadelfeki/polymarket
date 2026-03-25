# Archive

Files in this directory are superseded entry points and compatibility shims that are no longer part of the active production path.

The canonical entry point is `main.py`. Do not import from this directory in new code.

| File | Status | Notes |
|------|--------|-------|
| `main_capital_doubler.py` | Compatibility shim | Provides `CapitalDoublerBot` for legacy tests. Full runtime is in `main.py`. |
| `main_production.py` | Compatibility alias | `ProductionTradingBot = TradingSystem` from `main.py`. |
