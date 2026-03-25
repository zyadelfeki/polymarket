"""Compatibility shim — legacy import alias for root-level imports.

Do NOT add logic here. Import directly from services.execution_service_v2.
This file exists only so that code written before the services/ refactor
continues to work without modification.
"""

from services.execution_service_v2 import *  # noqa: F403
from services.execution_service_v2 import ExecutionServiceV2  # noqa: F401

__all__ = ["ExecutionServiceV2"]
