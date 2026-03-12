"""
Unit tests for ai/feedback_loop.py

Focuses on the _resolve_sync fix: forward scan, patch-in-place,
chronological order preserved, multi-market isolation.
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import ai.feedback_loop as fl
from ai.feedback_loop import _resolve_sync, _append_line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(market_id: str, ts: float, outcome=None, pnl=None) -> dict:
    return {
        "ts": ts,
        "market_id": market_id,
        "question": "Will BTC close above 80k?",
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
        "outcome": outcome,
        "pnl": pnl,
    }


def _write_records(path: Path, records: list) -> None:
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_records(path: Path) -> list:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_resolve_patches_last_unresolved(tmp_path):
    """resolve should patch the LAST unresolved record, not the first."""
    f = tmp_path / "decisions.jsonl"
    records = [
        _make_record("mkt_A", 1000.0),   # first unresolved
        _make_record("mkt_A", 1001.0),   # second unresolved — this should be patched
    ]
    _write_records(f, records)

    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_A", "WIN", 1.50)

    result = _read_records(f)
    assert len(result) == 2
    # first record should remain unresolved
    assert result[0]["outcome"] is None
    assert result[0]["ts"] == 1000.0
    # second (last) record should be patched
    assert result[1]["outcome"] == "WIN"
    assert result[1]["pnl"] == 1.50
    assert result[1]["ts"] == 1001.0


def test_resolve_preserves_chronological_order(tmp_path):
    """After resolve, file lines must remain in original ts order."""
    f = tmp_path / "decisions.jsonl"
    ts_values = [1000.0 + i for i in range(10)]
    records = [_make_record("mkt_X", ts) for ts in ts_values]
    _write_records(f, records)

    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_X", "LOSS", -0.80)

    result = _read_records(f)
    assert len(result) == 10
    actual_ts = [r["ts"] for r in result]
    assert actual_ts == sorted(actual_ts), "Chronological order was corrupted!"


def test_resolve_noop_when_no_unresolved(tmp_path):
    """resolve is a no-op if no unresolved record exists for market_id."""
    f = tmp_path / "decisions.jsonl"
    records = [_make_record("mkt_B", 1000.0, outcome="WIN", pnl=1.0)]
    _write_records(f, records)
    original_content = f.read_text()

    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_B", "LOSS", -1.0)  # already resolved

    assert f.read_text() == original_content


def test_resolve_skips_large_files(tmp_path):
    """resolve does nothing if file > 10 MB."""
    f = tmp_path / "decisions.jsonl"
    records = [_make_record("mkt_C", 1000.0)]
    _write_records(f, records)
    original_content = f.read_text()

    # Mock stat to return a size > 10 MB
    class FakeStat:
        st_size = 11 * 1024 * 1024

    with patch.object(fl, "DECISIONS_FILE", f):
        with patch.object(Path, "stat", return_value=FakeStat()):
            _resolve_sync("mkt_C", "WIN", 2.0)

    assert f.read_text() == original_content


def test_resolve_multi_market_isolation(tmp_path):
    """Resolving mkt_A must never touch mkt_B records."""
    f = tmp_path / "decisions.jsonl"
    records = [
        _make_record("mkt_A", 1000.0),
        _make_record("mkt_B", 1001.0),
        _make_record("mkt_A", 1002.0),
        _make_record("mkt_B", 1003.0),
    ]
    _write_records(f, records)

    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_A", "WIN", 1.0)

    result = _read_records(f)
    assert len(result) == 4
    mkt_b_records = [r for r in result if r["market_id"] == "mkt_B"]
    for r in mkt_b_records:
        assert r["outcome"] is None, "mkt_B was incorrectly modified!"

    mkt_a_records = [r for r in result if r["market_id"] == "mkt_A"]
    resolved_a = [r for r in mkt_a_records if r["outcome"] is not None]
    assert len(resolved_a) == 1  # only the last one resolved
    assert resolved_a[0]["ts"] == 1002.0  # the LAST one


def test_resolve_noop_when_file_missing(tmp_path):
    """resolve does not crash if decisions.jsonl doesn't exist."""
    f = tmp_path / "nonexistent.jsonl"
    with patch.object(fl, "DECISIONS_FILE", f):
        _resolve_sync("mkt_D", "WIN", 1.0)  # must not raise
