"""Compatibility import alias for legacy startup paths and tests.

The canonical runtime entry point is ``python main.py --config config/production.yaml --mode {paper|live}``.
This module stays intentionally tiny so existing imports of ``main_production.ProductionTradingBot``
continue to work without duplicating startup logic.
"""

from main import TradingSystem


ProductionTradingBot = TradingSystem