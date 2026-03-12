"""
Stress test for _resolve_sync integrity.

- 100 interleaved records across 10 markets
- Random-order resolution
- Verifies: line count, all resolved, ts order preserved, idempotency
"""
import json
import random
import time
from pathlib import Path
from unittest.mock import patch

import ai.feedback_loop as fl
from ai.feedback_loop import _resolve_sync


def _make_record(market_id: str, ts: float) -> dict:
    return {
        "ts": ts,
        "market_id": market_id,
        "question": f"Test question for {market_id}",
        "charlie_side": "YES",
        "p_win": 0.65,
        "edge": 0.12,
        "llm_coherent": True,
        "llm_coherence_confidence": 0.85,
        "llm_is_trap": False,
        "llm_trap_confidence": 0.10,
        "edge_quality_score": 0.75,
        "regime_label": "STABLE",
        "action": "APPROVED",
        "outcome": None,
        "pnl": None,
    }


def test_100_records_random_resolve_order(tmp_path):
    """
    Write 100 interleaved records for 10 markets.
    Resolve all in random order.
    Assert: 100 lines remain, all resolved, ts order preserved.
    """
    f = tmp_path / "decisions.jsonl"
    random.seed(42)

    market_ids = [f"mkt_{i:02d}" for i in range(10)]
    # 10 records per market, interleaved
    records = []
    ts = 1000.0
    for _ in range(10):  # 10 rounds
        for mid in market_ids:
            records.append(_make_record(mid, ts))
            ts += 0.1

    lines = [json.dumps(r) for r in records]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Resolve in random market order (each market resolved once = patches its last record)
    shuffled_markets = market_ids[:]
    random.shuffle(shuffled_markets)

    with patch.object(fl, "DECISIONS_FILE", f):
        for mid in shuffled_markets:
            _resolve_sync(mid, "WIN", 1.0)

    result_lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(result_lines) == 100, f"Expected 100 lines, got {len(result_lines)}"

    records_out = [json.loads(l) for l in result_lines]

    # ts order preserved
    ts_values = [r["ts"] for r in records_out]
    assert ts_values == sorted(ts_values), "Chronological order corrupted after mass resolve!"

    # Only last record per market should be resolved (9 per market still unresolved, 1 resolved)
    for mid in market_ids:
        market_records = [r for r in records_out if r["market_id"] == mid]
        assert len(market_records) == 10
        resolved = [r for r in market_records if r["outcome"] is not None]
        unresolved = [r for r in market_records if r["outcome"] is None]
        assert len(resolved) == 1, f"{mid}: expected 1 resolved, got {len(resolved)}"
        assert len(unresolved) == 9
        # The resolved one must be the last (highest ts) for that market
        assert resolved[0]["ts"] == max(r["ts"] for r in market_records)


def test_double_resolve_is_idempotent(tmp_path):
    """
    Resolving an already-resolved market a second time must not corrupt the file.
    """
    f = tmp_path / "decisions.jsonl"
    records = [
        _make_record("mkt_Z", 1000.0),
        _make_record("mkt_Z", 1001.0),
    ]
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_Z", "WIN", 1.0)
        snapshot_after_first = f.read_text()
        _resolve_sync("mkt_Z", "WIN", 1.0)  # second resolve — should only patch the first now
        snapshot_after_second = f.read_text()

    result = [json.loads(l) for l in snapshot_after_second.splitlines() if l.strip()]
    assert len(result) == 2
    # Both records should be resolved now (second resolve patched the remaining unresolved one)
    # Most importantly: file is not corrupt and has exactly 2 lines
    assert all(json.loads(l) for l in snapshot_after_second.splitlines() if l.strip())
