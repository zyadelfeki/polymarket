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


def test_run_ps1_targets_main_py():
    content = (REPO_ROOT / "run.ps1").read_text(encoding="utf-8")
    assert "main.py --config config/production.yaml --mode $Mode" in content
    assert "main_v2.py" not in content


def test_run_bat_targets_main_py():
    content = (REPO_ROOT / "run.bat").read_text(encoding="utf-8")
    assert "main.py --config config/production.yaml --mode paper" in content
    assert "main_v2.py" not in content


def test_find_open_positions_reports_open_orders_and_positions_read_only(tmp_path, monkeypatch, capsys):
    module = _load_script_module("find_open_positions_script", "scripts/find_open_positions.py")
    config_path = _write_config(tmp_path / "config.yaml")

    class StubClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_open_orders(self):
            return [{"order_id": "ord-1", "market_id": "m-1", "status": "OPEN"}]

        async def get_open_positions(self):
            return [{"market_id": "m-1", "token_id": "token-1", "quantity": "2"}]

    monkeypatch.setattr(module, "PolymarketClientV2", StubClient)

    exit_code = asyncio.run(module.main_async(["--config", str(config_path)]))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "OPEN ORDERS" in captured.out
    assert "ord-1" in captured.out
    assert "OPEN POSITIONS" in captured.out
    assert "token-1" in captured.out


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
    assert calls == {"initialized": 1, "reconciled": 1, "closed": 1}
    assert "RECONCILIATION SUMMARY" in captured.out
    assert '"open_orders": 3' in captured.out
    assert '"resolved_while_offline": 1' in captured.out