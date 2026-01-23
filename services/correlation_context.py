"""Correlation context propagation utilities."""

from typing import Optional, Dict, Any

from utils.correlation_id import (
    get_correlation_id,
    set_correlation_id,
    use_correlation_id,
    inject_correlation,
)


class CorrelationContext:
    """Context manager for correlation IDs."""

    @staticmethod
    def get() -> Optional[str]:
        return get_correlation_id()

    @staticmethod
    def set(correlation_id: Optional[str]) -> None:
        set_correlation_id(correlation_id)

    @staticmethod
    def use(correlation_id: Optional[str]):
        return use_correlation_id(correlation_id)


__all__ = ["CorrelationContext", "inject_correlation"]
