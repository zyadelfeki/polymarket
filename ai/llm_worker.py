"""Background LLM inference worker.

Architecture
------------
* A single module-level ``LLMCache`` instance (``_cache``) is shared between
  this worker and the scanner.
* ``_singleton_worker`` holds the one running ``LLMWorker`` instance so that
  ``main.py`` can reference it after creation and ``btc_price_level_scanner``
  can enqueue candidates via a simple module-attribute access.

Design invariants
-----------------
* Cache miss in the scanner  → silent pass-through (never blocks a trade).
* Worker crash / slow model  → logged, trading continues unaffected.
* Queue full                 → oldest entry dropped; newest queued (LIFO pressure
  relief preserves recency).
* No position-sizing logic lives here; this module only fills the cache.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import structlog

from ai.llm_cache import LLMCache

logger = structlog.get_logger(__name__)

# --- module-level singletons --------------------------------------------------

_cache: LLMCache = LLMCache()
_singleton_worker: Optional["LLMWorker"] = None


def get_cache() -> LLMCache:
    """Return the shared module-level cache instance."""
    return _cache


# --- worker -------------------------------------------------------------------

class LLMWorker:
    """Async worker that processes LLM inference requests and populates the cache.

    Intended to run as a background asyncio task created via
    ``asyncio.get_running_loop().create_task(worker.run())``.  It processes one
    candidate per iteration; the blocking model call is entirely within the
    ``_process`` coroutine so it yields the event loop between requests.

    Args:
        maxsize: Maximum pending queue depth.  When the queue is full the oldest
                 entry is evicted to make room for the newest.
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._running = False

    # ------------------------------------------------------------------
    # Public interface used by the scanner
    # ------------------------------------------------------------------

    def enqueue(self, candidates: List[Dict[str, Any]]) -> None:
        """Non-blocking enqueue.  Drops the oldest entry if the queue is full."""
        for candidate in candidates:
            try:
                self._queue.put_nowait(candidate)
            except asyncio.QueueFull:
                # Drop oldest, make room for the freshest signal.
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(candidate)
                except Exception:
                    pass  # If even this fails, skip silently.

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main event loop.  Runs until ``stop()`` is called."""
        self._running = True
        logger.info("llm_worker_loop_started")
        while self._running:
            try:
                candidate = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                await self._process(candidate)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue  # idle poll — keep looping
            except Exception as exc:
                logger.warning("llm_worker_loop_error", error=str(exc))

    def stop(self) -> None:
        """Signal the run loop to exit after its current iteration."""
        self._running = False

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    async def _process(self, candidate: Dict[str, Any]) -> None:
        """Run full LLM pipeline for one candidate and store result in cache.

        Failures are caught and logged; the cache entry is simply not written,
        which results in a pass-through on the next scanner iteration.
        """
        market_id = candidate.get("market_id", "")
        question  = candidate.get("question", "")
        try:
            from ai.market_parser import parse_market_question
            from ai.signal_enricher import anomaly_veto, check_coherence

            ctx = await parse_market_question(question)

            btc_price    = float(candidate.get("btc_price", 0))
            market_price = float(candidate.get("market_price", 0.5))
            rsi          = float(candidate.get("rsi", 50))
            macd_val     = float(candidate.get("macd", 0))
            charlie_side = str(candidate.get("charlie_side", "YES"))
            p_win        = float(candidate.get("p_win", 0.5))

            anomaly = await anomaly_veto(
                question, btc_price, market_price=market_price
            )
            coherence = await check_coherence(
                context=ctx,
                btc_price=btc_price,
                rsi=rsi,
                macd_val=macd_val,
                charlie_side=charlie_side,
                p_win=p_win,
                market_price=market_price,
            )

            await _cache.set(market_id, question, (anomaly, coherence))
            logger.debug("llm_worker_cache_updated", market_id=market_id)

        except Exception as exc:
            logger.warning(
                "llm_worker_process_error", market_id=market_id, error=str(exc)
            )
