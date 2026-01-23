import asyncio
import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from utils.correlation_id import get_correlation_id, use_correlation_id


@pytest.mark.asyncio
async def test_correlation_id_isolated_between_tasks():
    results = []

    async def worker(cid: str):
        with use_correlation_id(cid):
            await asyncio.sleep(0)
            results.append(get_correlation_id())

    await asyncio.gather(worker("corr_a"), worker("corr_b"))
    assert set(results) == {"corr_a", "corr_b"}
