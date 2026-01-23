import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.error_codes import ErrorCode, OperationalError, ValidationError
from services.retry import RetryableOperation


@pytest.mark.asyncio
async def test_retry_only_operational_errors():
    attempts = {"count": 0}

    async def operation():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise OperationalError(ErrorCode.NETWORK_ERROR, "temporary")
        return "ok"

    result = await RetryableOperation.run(operation, max_retries=3, base_delay=0.0, jitter=0.0)
    assert result == "ok"
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_validation_error_not_retried():
    attempts = {"count": 0}

    async def operation():
        attempts["count"] += 1
        raise ValidationError(ErrorCode.INVALID_ORDER, "bad input")

    with pytest.raises(ValidationError):
        await RetryableOperation.run(operation, max_retries=3, base_delay=0.0, jitter=0.0)

    assert attempts["count"] == 1


def test_trading_exception_to_dict():
    err = ValidationError(ErrorCode.INVALID_ORDER, "bad input", metadata={"field": "price"})
    payload = err.to_dict()
    assert payload["error_code"] == ErrorCode.INVALID_ORDER.value
    assert payload["metadata"]["field"] == "price"
    assert payload["retryable"] is False
