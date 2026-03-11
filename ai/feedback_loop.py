"""
Lightweight feedback loop for LLM decision tracking.
Appends one JSON line per LLM decision to ai/decisions.jsonl.
At trade settlement, a separate resolve() call updates the outcome.
Non-blocking: all writes are fire-and-forget via asyncio.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

DECISIONS_FILE = Path(__file__).parent / "decisions.jsonl"


async def record_decision(
    *,
    market_id: str,
    question: str,
    charlie_side: str,
    p_win: float,
    edge: float,
    llm_coherent: Optional[bool],
    llm_coherence_confidence: Optional[float],
    llm_is_trap: Optional[bool],
    llm_trap_confidence: Optional[float],
    edge_quality_score: Optional[float],
    regime_label: str,
    action: str,  # "APPROVED", "VETOED_COHERENCE", "VETOED_TRAP", "VETOED_REGIME"
) -> None:
    """Fire-and-forget: append one line to decisions.jsonl."""
    record = {
        "ts": time.time(),
        "market_id": market_id,
        "question": question[:120],
        "charlie_side": charlie_side,
        "p_win": round(p_win, 4),
        "edge": round(edge, 4),
        "llm_coherent": llm_coherent,
        "llm_coherence_confidence": llm_coherence_confidence,
        "llm_is_trap": llm_is_trap,
        "llm_trap_confidence": llm_trap_confidence,
        "edge_quality_score": edge_quality_score,
        "regime_label": regime_label,
        "action": action,
        "outcome": None,  # filled in by resolve()
        "pnl": None,      # filled in by resolve()
    }
    asyncio.get_running_loop().run_in_executor(None, _append_line, json.dumps(record))


def _append_line(line: str) -> None:
    try:
        with open(DECISIONS_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # never block on log failure


async def resolve_decision(market_id: str, outcome: str, pnl: float) -> None:
    """
    Called at trade settlement to update outcome.
    Rewrites the last matching unresolved record in-place (in memory then full rewrite).
    Only called if file is small enough (< 10 MB) — skip otherwise.
    """
    asyncio.get_running_loop().run_in_executor(None, _resolve_sync, market_id, outcome, pnl)


def _resolve_sync(market_id: str, outcome: str, pnl: float) -> None:
    try:
        if not DECISIONS_FILE.exists():
            return
        if DECISIONS_FILE.stat().st_size > 10 * 1024 * 1024:
            return  # skip large files
        lines = DECISIONS_FILE.read_text(encoding="utf-8").splitlines()
        updated = []
        resolved = False
        for line in reversed(lines):
            if not resolved:
                try:
                    rec = json.loads(line)
                    if rec.get("market_id") == market_id and rec.get("outcome") is None:
                        rec["outcome"] = outcome
                        rec["pnl"] = pnl
                        updated.insert(0, json.dumps(rec))
                        resolved = True
                        continue
                except Exception:
                    pass
            updated.insert(0, line)
        DECISIONS_FILE.write_text("\n".join(updated) + "\n", encoding="utf-8")
    except Exception:
        pass
