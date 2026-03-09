from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script_module(module_name: str, relative_path: str):
    script_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_config(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "trading": {"paper_trading": True},
                "api": {"polymarket": {"rate_limit": 8.0, "timeout_seconds": 10.0, "max_retries": 3}},
                "database": {"path": str(path.parent / "test.db")},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


async def _seed_open_state_db(db_path: Path) -> None:
    from database.ledger_async import AsyncLedger

    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()
    try:
        await ledger.execute(
            """
            INSERT INTO order_tracking (
                order_id, market_id, token_id, outcome, side, size, price,
                order_state, opened_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ord-local-1",
                "m-local-1",
                "token-local-1",
                "YES",
                "BUY",
                "10",
                "0.40",
                "SUBMITTED",
                "2026-03-08T00:00:00+00:00",
                "seeded_local_order",
            ),
            commit=True,
        )
        await ledger.execute(
            """
            INSERT INTO positions (
                market_id, token_id, strategy, side, entry_price, quantity,
                current_price, unrealized_pnl, status, entry_timestamp, opened_at,
                entry_order_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
            """,
            (
                "m-local-1",
                "token-local-1",
                "seeded_strategy",
                "YES",
                "0.40",
                "25",
                "0.55",
                "3.75",
                "2026-03-08T00:00:00+00:00",
                "2026-03-08T00:00:00+00:00",
                "ord-local-1",
                "{}",
            ),
            commit=True,
        )
    finally:
        await ledger.close()


def test_run_ps1_targets_main_py():
    content = (REPO_ROOT / "run.ps1").read_text(encoding="utf-8")
    assert "main.py --config config/production.yaml --mode $Mode" in content
    assert "main_v2.py" not in content


def test_run_bat_targets_main_py():
    content = (REPO_ROOT / "run.bat").read_text(encoding="utf-8")
    assert "main.py --config config/production.yaml --mode paper" in content
    assert "main_v2.py" not in content


def test_find_open_positions_reports_local_sources_and_gaps(tmp_path, capsys):
    module = _load_script_module("find_open_positions_script", "scripts/find_open_positions.py")
    config_path = _write_config(tmp_path / "config.yaml")

    db_path = tmp_path / "test.db"
    asyncio.run(_seed_open_state_db(db_path))

    exit_code = asyncio.run(module.main_async(["--config", str(config_path), "--exchange", "off"]))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "DISCOVERED SOURCES" in captured.out
    assert "OPEN ORDERS" in captured.out
    assert "ord-local-1" in captured.out
    assert "POSITIONS" in captured.out
    assert "token-local-1" in captured.out
    assert "exchange lookup disabled (--exchange off)" in captured.out


def test_find_open_positions_reports_exchange_only_rows_and_safe_unrealized_pnl(tmp_path, monkeypatch, capsys):
    module = _load_script_module("find_open_positions_script_live", "scripts/find_open_positions.py")
    config_path = _write_config(tmp_path / "config.yaml")
    created_clients = []

    class StubClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_clients.append(self)

        async def get_open_orders(self):
            return [
                {
                    "order_id": "ord-ex-1",
                    "market_id": "m-ex-1",
                    "token_id": "token-ex-yes",
                    "outcome": "YES",
                    "side": "BUY",
                    "size": "10",
                    "price": "0.40",
                    "status": "SUBMITTED",
                    "opened_at": "2026-03-08T00:00:00+00:00",
                }
            ]

        async def get_open_positions(self):
            return [
                {
                    "market_id": "m-ex-1",
                    "token_id": "token-ex-yes",
                    "quantity": "25",
                    "avg_price": "0.40",
                    "side": "YES",
                }
            ]

        async def get_market(self, market_id: str):
            assert market_id == "m-ex-1"
            return {
                "market_id": market_id,
                "yes_token_id": "token-ex-yes",
                "no_token_id": "token-ex-no",
                "yes_price": "0.55",
                "no_price": "0.45",
            }

        async def close(self):
            return None

    monkeypatch.setattr(module, "PolymarketClientV2", StubClient)

    exit_code = asyncio.run(module.main_async(["--config", str(config_path), "--exchange", "live"]))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert created_clients and created_clients[0].kwargs["paper_trading"] is False
    assert "exchange_open_order_missing_from_local_tracking" in captured.out
    assert "exchange_position_missing_from_local_ledger" in captured.out
    assert '"unrealized_pnl": "3.75"' in captured.out


def test_reconcile_positions_calls_existing_reconcile_and_prints_summary(tmp_path, monkeypatch, capsys):
    module = _load_script_module("reconcile_positions_script", "scripts/reconcile_positions.py")
    config_path = _write_config(tmp_path / "config.yaml")
    calls = {"initialized": 0, "reconciled": 0, "closed": 0}

    class StubLedger:
        def __init__(self, db_path: str):
            self.db_path = db_path

        async def initialize(self):
            calls["initialized"] += 1

        async def reconcile_open_orders(self, api_client):
            calls["reconciled"] += 1
            return {
                "open_orders": 3,
                "resolved_while_offline": 1,
                "still_open": 2,
                "recovered_pnl": "1.25",
            }

        async def close(self):
            calls["closed"] += 1

    class StubClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(module, "AsyncLedger", StubLedger)
    monkeypatch.setattr(module, "PolymarketClientV2", StubClient)

    exit_code = asyncio.run(module.main_async(["--config", str(config_path)]))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == {"initialized": 2, "reconciled": 1, "closed": 2}
    assert "RECONCILIATION SUMMARY" in captured.out
    assert '"open_orders": 3' in captured.out
    assert '"resolved_while_offline": 1' in captured.out