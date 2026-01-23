"""Correlation ID context utilities and logging helpers."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional


_correlation_id: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)


def generate_correlation_id() -> str:
    return str(uuid.uuid4())


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def set_correlation_id(correlation_id: Optional[str]) -> None:
    _correlation_id.set(correlation_id)


@contextmanager
def use_correlation_id(correlation_id: Optional[str]):
    token = _correlation_id.set(correlation_id)
    try:
        yield
    finally:
        _correlation_id.reset(token)


def inject_correlation(payload: Dict[str, Any]) -> Dict[str, Any]:
    if payload.get("correlation_id") is None:
        payload["correlation_id"] = get_correlation_id()
    return payload


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id") or record.correlation_id is None:
            record.correlation_id = get_correlation_id() or "-"
        return True


def structlog_correlation_processor(logger, method_name, event_dict):
    if event_dict.get("correlation_id") is None:
        event_dict["correlation_id"] = get_correlation_id()
    return event_dict
