from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import json
from unittest.mock import AsyncMock
from pathlib import Path

import joblib
import pytest

from ml import meta_promotion, meta_training
from database.ledger_async import AsyncLedger
from main import TradingSystem, _resolve_runtime_controls
from tests.test_meta_training import _make_training_rows, _write_validated_inputs
from ml.meta_gate import MetaRuntimeDecision
from services.execution_service_v2 import OrderResult, OrderStatus


class CaptureCharlieGate:
    def __init__(self, result=None):
        self.calls = []
        self._result = result

    async def evaluate_market(self, **kwargs):
        self.calls.append(kwargs)
        return self._result


class DummyRec:
    def __init__(self, side: str = "YES"):
        self.side = side
        self.size = Decimal("10")
        self.kelly_fraction = Decimal("0.1")
        self.p_win = 0.62
        self.p_win_raw = 0.64
        self.p_win_calibrated = 0.62
        self.implied_prob = 0.5
        self.edge = 0.08
        self.confidence = 0.8
        self.regime = "BULLISH"
        self.technical_regime = "TRENDING"
        self.reason = "charlie_signal side=YES p_win=0.620 implied=0.500 edge=0.080 fee=0.010 conf=0.800"
        self.model_votes = None
        self.ofi_conflict = False


class StaticApiClient:
    def __init__(self, market_response):
        self.market_response = market_response

    async def get_market(self, market_id):
        return self.market_response


@pytest.fixture
def base_config() -> dict:
    return _resolve_runtime_controls(
        {
            "environment": "test",
            "trading": {
                "paper_trading": True,
                "min_price": 0.01,
                "max_price": 0.99,
                "max_position_size_pct": 10.0,
                "min_position_size": 1.0,
                "max_order_size": 1000.0,
                "max_entry_price_abs": "0.65",
            },
            "strategies": {
                "latency_arb": {
                    "max_position_size_pct": 5.0,
                }
            },
            "startup": {
                "strategy_scan_min_interval_seconds": 0.0,
                "strategy_scan_timeout_seconds": 2.0,
                "network_timeout_seconds": 2.0,
            },
            "runtime_controls": {
                "blocked_markets": [1487045],
                "lifecycle_guard": {
                    "enabled": True,
                    "max_active_entries_per_market": 1,
                    "allow_add_on": False,
                    "min_price_improvement_abs": "0.05",
                },
                "calibration": {
                    "fail_closed": True,
                    "min_positive_coef": 0.0,
                    "require_monotonic_smoke_test": True,
                    "smoke_test_points": [0.30, 0.50, 0.79],
                    "observe_only_on_invalid": True,
                    "dataset_export_path": "data/test_calibration_dataset_v2.csv",
                    "observation_export_path": "data/test_calibration_observations.csv",
                },
                "quarantine": {
                    "enabled": True,
                    "seed_static_blocklist": True,
                    "auto_review_after_days": 7,
                },
            },
        }
    )


async def _attach_sqlite_quarantine_store(system: TradingSystem, db_path) -> AsyncLedger:
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()
    ledger.get_equity = AsyncMock(return_value=Decimal("100"))
    system.ledger = ledger
    await system._initialize_quarantine_store()
    return ledger


async def _attach_sqlite_runtime_store(system: TradingSystem, db_path) -> AsyncLedger:
    ledger = AsyncLedger(db_path=str(db_path))
    await ledger.initialize()
    ledger.get_equity = AsyncMock(return_value=Decimal("100"))
    system.ledger = ledger
    system._ensure_runtime_services()
    return ledger


@pytest.mark.asyncio
async def test_blocked_market_ids_are_coerced_to_strings(base_config):
    system = TradingSystem(base_config)
    assert "1487045" in system.blocked_markets
    assert 1487045 not in system.blocked_markets


@pytest.mark.asyncio
async def test_bad_calibration_blocks_before_charlie(base_config):
    system = TradingSystem(base_config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.charlie_gate = CaptureCharlieGate(result=DummyRec())
    system._calibration_guard_status = {
        "blocked": True,
        "reason": "non_positive_coef=-0.1",
        "coef": -0.1,
        "monotonic": False,
    }

    await system._execute_opportunity(
        {
            "market_id": "market-1",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.50"),
            "confidence": "HIGH",
        },
        trigger="test",
    )

    assert system._session_stats["blocked_bad_calibration"] == 1
    assert len(system.charlie_gate.calls) == 1
    assert system.execution.place_order_with_risk_check.await_count == 0


@pytest.mark.asyncio
async def test_charlie_receives_normalized_yes_price_for_no_opportunity(base_config):
    system = TradingSystem(base_config)
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.charlie_gate = CaptureCharlieGate(result=None)

    await system._execute_opportunity(
        {
            "market_id": "market-2",
            "token_id": "token-no",
            "side": "NO",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.73"),
            "confidence": "HIGH",
            "question": "Will BTC close above 100k?",
        },
        trigger="test",
    )

    assert len(system.charlie_gate.calls) == 1
    assert system.charlie_gate.calls[0]["market_price"] == Decimal("0.27")


@pytest.mark.asyncio
async def test_charlie_receives_yes_price_for_yes_opportunity(base_config):
    system = TradingSystem(base_config)
    system.execution = AsyncMock()
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.charlie_gate = CaptureCharlieGate(result=None)

    await system._execute_opportunity(
        {
            "market_id": "market-3",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.41"),
            "confidence": "HIGH",
            "question": "Will BTC close above 100k?",
        },
        trigger="test",
    )

    assert len(system.charlie_gate.calls) == 1
    assert system.charlie_gate.calls[0]["market_price"] == Decimal("0.41")


@pytest.mark.asyncio
async def test_charlie_unavailable_blocks_trade_submission(base_config):
    system = TradingSystem(base_config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker = AsyncMock()
    system.circuit_breaker.can_trade = AsyncMock(return_value=True)

    await system._execute_opportunity(
        {
            "market_id": "market-charlie-missing",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.50"),
            "confidence": "HIGH",
        },
        trigger="test",
    )

    assert system.execution.place_order_with_risk_check.await_count == 0
    assert system._session_stats["blocked_charlie_rejected"] == 1


@pytest.mark.asyncio
async def test_charlie_side_flip_without_explicit_token_mapping_blocks_trade(base_config):
    system = TradingSystem(base_config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.circuit_breaker = AsyncMock()
    system.circuit_breaker.can_trade = AsyncMock(return_value=True)
    system.charlie_gate = CaptureCharlieGate(result=DummyRec(side="NO"))

    await system._execute_opportunity(
        {
            "market_id": "market-side-flip",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.41"),
            "confidence": "HIGH",
            "question": "Will BTC close above 100k?",
        },
        trigger="test",
    )

    assert system.execution.place_order_with_risk_check.await_count == 0
    assert system._session_stats["blocked_charlie_rejected"] == 1


@pytest.mark.asyncio
async def test_paper_scan_does_not_leave_open_order_when_charlie_rejects(tmp_path, base_config):
    system = TradingSystem(base_config)
    ledger = await _attach_sqlite_runtime_store(system, tmp_path / "runtime.db")
    try:
        system.execution = AsyncMock()
        system.execution.place_order_with_risk_check = AsyncMock()
        system.circuit_breaker = AsyncMock()
        system.circuit_breaker.can_trade = AsyncMock(return_value=True)
        system.strategy_engine = AsyncMock()
        system.charlie_gate = CaptureCharlieGate(result=None)
        system.strategy_engine.scan_opportunities = AsyncMock(
            return_value={
                "market_id": "market-paper-reject",
                "token_id": "token-yes",
                "side": "YES",
                "edge": Decimal("0.05"),
                "market_price": Decimal("0.50"),
                "confidence": "HIGH",
                "question": "Will BTC close above 100k?",
            }
        )

        await system._run_strategy_scan(trigger="unit_test")

        open_orders = await ledger.get_open_orders()
        assert open_orders == []
        assert system.execution.place_order_with_risk_check.await_count == 0
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_lifecycle_guard_blocks_side_flip_and_allows_improved_add_on(base_config):
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "lifecycle_guard": {
                "enabled": True,
                "max_active_entries_per_market": 2,
                "allow_add_on": True,
                "min_price_improvement_abs": "0.05",
            },
        }
    })
    system = TradingSystem(config)
    system._record_lifecycle_entry(
        market_id="market-4",
        side="YES",
        token_id="token-yes",
        token_price=Decimal("0.55"),
        order_id="ord-1",
    )

    blocked, reason, _ = system._check_lifecycle_guard(
        market_id="market-4",
        side="NO",
        token_price=Decimal("0.40"),
    )
    assert blocked is True
    assert reason == "side_flip_rule"

    blocked, reason, context = system._check_lifecycle_guard(
        market_id="market-4",
        side="YES",
        token_price=Decimal("0.53"),
    )
    assert blocked is True
    assert reason == "lifecycle_guard"
    assert context["actual_improvement"] == "0.02"

    blocked, reason, _ = system._check_lifecycle_guard(
        market_id="market-4",
        side="YES",
        token_price=Decimal("0.49"),
    )
    assert blocked is False
    assert reason == ""


@pytest.mark.asyncio
async def test_clear_lifecycle_entry_removes_market(base_config):
    system = TradingSystem(base_config)
    system._record_lifecycle_entry(
        market_id="market-5",
        side="YES",
        token_id="token-yes",
        token_price=Decimal("0.52"),
        order_id="ord-5",
    )

    system._clear_lifecycle_entry("market-5", reason="test")

    assert "market-5" not in system._market_lifecycle_state


@pytest.mark.asyncio
async def test_observe_only_bad_calibration_records_observation(tmp_path, base_config):
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "dataset_export_path": str(tmp_path / "calibration_dataset_v2.csv"),
                "observation_export_path": str(tmp_path / "calibration_observations.csv"),
            },
            "quarantine": {
                **base_config["runtime_controls"]["quarantine"],
            },
        },
    })
    system = TradingSystem(config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.charlie_gate = CaptureCharlieGate(result=DummyRec())
    system._calibration_guard_status = {
        "blocked": True,
        "reason": "non_positive_coef=-0.1",
        "coef": -0.1,
        "monotonic": False,
    }
    ledger = await _attach_sqlite_runtime_store(system, tmp_path / "observe_only.db")

    try:
        await system._execute_opportunity(
            {
                "market_id": "market-observe",
                "token_id": "token-yes",
                "side": "YES",
                "edge": Decimal("0.05"),
                "market_price": Decimal("0.41"),
                "confidence": "HIGH",
                "question": "Will BTC settle up?",
            },
            trigger="test",
        )

        rows = await ledger.execute(
            "SELECT observation_mode, market_id, guard_block_reason FROM calibration_observations",
            fetch_all=True,
            as_dict=True,
        )
        await system.calibration_observation_service.export_csv_artifacts(observations=True, dataset=False)

        with open(tmp_path / "calibration_observations.csv", newline="", encoding="utf-8") as observation_file:
            exported_rows = list(csv.DictReader(observation_file))

        assert len(rows) == 1
        assert rows[0]["observation_mode"] == "observe_only_bad_calibration"
        assert rows[0]["market_id"] == "market-observe"
        assert rows[0]["guard_block_reason"] == "bad_calibration"
        assert exported_rows[0]["yes_side_raw_probability"] == exported_rows[0]["raw_yes_prob"]
        assert system.execution.place_order_with_risk_check.await_count == 0
        assert system._session_stats["observe_only_bad_calibration"] == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_meta_shadow_mode_does_not_change_trade_submission(tmp_path, base_config, monkeypatch):
    events = []

    class CaptureLogger:
        def info(self, event, **fields):
            events.append(("info", event, fields))

        def warning(self, event, **fields):
            events.append(("warning", event, fields))

        def error(self, event, **fields):
            events.append(("error", event, fields))

        def debug(self, event, **fields):
            events.append(("debug", event, fields))

    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "dataset_export_path": str(tmp_path / "calibration_dataset_v2.csv"),
                "observation_export_path": str(tmp_path / "calibration_observations.csv"),
            },
            "quarantine": {
                **base_config["runtime_controls"]["quarantine"],
            },
        },
    })
    system = TradingSystem(config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock(
        return_value=OrderResult(
            success=True,
            order_id="shadow-ord-1",
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("24.39"),
            filled_price=Decimal("0.41"),
            fees=Decimal("0.01"),
        )
    )
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.charlie_gate = CaptureCharlieGate(result=DummyRec())
    monkeypatch.setattr("main.logger", CaptureLogger())

    monkeypatch.setattr(
        "ml.meta_gate.evaluate_runtime_decision",
        lambda *args, **kwargs: MetaRuntimeDecision(
            allow_trade=False,
            p_profit=0.12,
            expected_return_bps=80.0,
            size_multiplier=1.0,
            block_reason="p_profit_below_threshold",
            model_version="meta-shadow-v1",
            feature_schema_version="meta_candidate_v1",
            calibration_version="platt_scaler_v1",
            decision_mode="shadow",
            shadow_only=True,
            training_eligibility="shadow_pending_outcome",
            integrity_flags=["shadow_only_no_trade_impact", "shadow_rejection_no_trade_impact"],
        ),
    )

    await system._execute_opportunity(
        {
            "market_id": "market-shadow",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.41"),
            "confidence": "HIGH",
            "question": "Will BTC settle up?",
        },
        trigger="test",
    )

    assert system.execution.place_order_with_risk_check.await_count == 1
    assert system._session_stats["blocked_meta_gate"] == 0
    assert "meta_shadow_decisions" not in system._session_stats
    assert "meta_shadow_rejections" not in system._session_stats
    assert all(event != "meta_gate_shadow_runtime_decision" for _, event, _ in events)
    assert any(event == "order_submission_attempt" for _, event, _ in events)


@pytest.mark.asyncio
async def test_promoted_shadow_runtime_scoring_remains_observational_only(tmp_path, base_config, monkeypatch):
    import ml.meta_gate as mg

    rows = _make_training_rows(120)
    for index, row in enumerate(rows):
        if index % 7 == 0:
            flipped_label = 1 - int(row["profitability_label"])
            row["profitability_label"] = flipped_label
            row["actual_yes_outcome"] = str(flipped_label)
            row["eventual_yes_market_outcome"] = str(flipped_label)
            row["settled_pnl"] = "2.00000000" if flipped_label else "-1.00000000"
            row["realized_return_bps"] = "200.000000" if flipped_label else "-100.000000"

    inputs_dir = tmp_path / "runtime-shadow-inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    executed_path, split_manifest_path, _ = _write_validated_inputs(inputs_dir, rows=rows)
    staging_dir = tmp_path / "runtime-shadow-staging"
    final_dir = tmp_path / "runtime-shadow-final"
    meta_training.write_training_artifacts(
        executed_profitability_path=str(executed_path),
        split_manifest_path=str(split_manifest_path),
        output_dir=str(staging_dir),
        run_id="runtime-shadow-stage",
        created_at="2026-03-07T08:00:00Z",
        random_state=42,
    )
    report = meta_promotion.finalize_staged_training(
        staging_dir=str(staging_dir),
        output_dir=str(final_dir),
        run_id="runtime-shadow-final",
        created_at="2026-03-07T08:10:00Z",
    )
    bundle_path = Path(report["promotable_model_bundle_path"])
    report_path = Path(report["training_report_path"])
    bundle = joblib.load(bundle_path)
    bundle["selected_threshold"] = 0.99
    joblib.dump(bundle, bundle_path)

    events = []

    class CaptureLogger:
        def info(self, event, **fields):
            events.append(("info", event, fields))

        def warning(self, event, **fields):
            events.append(("warning", event, fields))

        def error(self, event, **fields):
            events.append(("error", event, fields))

        def debug(self, event, **fields):
            events.append(("debug", event, fields))

    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "dataset_export_path": str(tmp_path / "calibration_dataset_v2.csv"),
                "observation_export_path": str(tmp_path / "calibration_observations.csv"),
            },
            "quarantine": {
                **base_config["runtime_controls"]["quarantine"],
            },
        },
    })
    system = TradingSystem(config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock(
        return_value=OrderResult(
            success=True,
            order_id="shadow-live-ord-1",
            status=OrderStatus.FILLED,
            filled_quantity=Decimal("24.39"),
            filled_price=Decimal("0.41"),
            fees=Decimal("0.01"),
        )
    )
    system.ledger = AsyncMock()
    system.ledger.get_equity.return_value = Decimal("100")
    system.charlie_gate = CaptureCharlieGate(result=DummyRec())

    monkeypatch.setattr("main.logger", CaptureLogger())
    monkeypatch.setattr(mg, "logger", CaptureLogger())
    monkeypatch.setattr(mg, "_PROMOTABLE_MODEL_BUNDLE_PATH", bundle_path)
    monkeypatch.setattr(mg, "_PROMOTION_REPORT_PATH", report_path)
    monkeypatch.setattr(mg, "_SHADOW_MODEL_CACHE", mg._NOT_LOADED)
    monkeypatch.setattr(mg, "_SHADOW_MODEL_LOAD_ATTEMPTED", False)
    monkeypatch.setattr(mg, "_meta_gate_shadow_load_successes", 0)
    monkeypatch.setattr(mg, "_meta_gate_shadow_load_failures", 0)
    monkeypatch.setattr(mg, "_meta_gate_shadow_load_failure_reasons", {})
    monkeypatch.setattr(mg, "_meta_gate_shadow_last_load_failure_reason", None)
    monkeypatch.setattr(
        "ml.meta_gate.extract_features_from_opportunity",
        lambda **kwargs: {
            "selected_side_is_yes": 1.0,
            "raw_yes_prob": 0.71,
            "yes_side_raw_probability": 0.71,
            "calibrated_yes_prob": 0.67,
            "selected_side_prob": 0.67,
            "charlie_confidence": 0.82,
            "charlie_implied_prob": 0.49,
            "charlie_edge": 0.08,
            "spread_bps": 90.0,
            "time_to_expiry_seconds": 3200.0,
            "token_price": 0.43,
            "normalized_yes_price": 0.43,
        },
    )

    await system._execute_opportunity(
        {
            "market_id": "market-shadow-runtime",
            "token_id": "token-yes",
            "side": "YES",
            "edge": Decimal("0.05"),
            "market_price": Decimal("0.41"),
            "confidence": "HIGH",
            "question": "Will BTC settle up?",
        },
        trigger="test",
    )

    assert system.execution.place_order_with_risk_check.await_count == 1
    assert system._session_stats["blocked_meta_gate"] == 0
    assert "meta_shadow_decisions" not in system._session_stats
    assert "meta_shadow_rejections" not in system._session_stats
    assert all(event != "meta_gate_shadow_runtime_decision" for _, event, _ in events)
    assert all(event != "meta_gate_shadow_artifact_load_success" for _, event, _ in events)
    assert any(event == "order_submission_attempt" for _, event, _ in events)


@pytest.mark.asyncio
async def test_pending_observation_resolution_appends_schema_v2_dataset(tmp_path, base_config):
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "dataset_export_path": str(tmp_path / "calibration_dataset_v2.csv"),
                "observation_export_path": str(tmp_path / "calibration_observations.csv"),
            },
            "quarantine": {
                **base_config["runtime_controls"]["quarantine"],
            },
        },
    })
    system = TradingSystem(config)
    system.api_client = StaticApiClient({"outcomePrices": ["1", "0"], "winning_side": "YES"})
    ledger = await _attach_sqlite_runtime_store(system, tmp_path / "resolve.db")

    try:
        await system._record_calibration_observation(
            market_id="market-resolve",
            token_id="token-yes",
            opportunity={"side": "YES", "question": "Will BTC settle up?"},
            charlie_rec=DummyRec(side="YES"),
            token_price=Decimal("0.42"),
            normalized_yes_price=Decimal("0.42"),
            trigger="test",
            observation_mode="trade_enabled",
        )

        await system._resolve_pending_calibration_observations()
        rows = await ledger.execute(
            "SELECT schema_version, actual_yes_outcome, eventual_yes_market_outcome FROM (SELECT '2' AS schema_version, actual_yes_outcome, eventual_yes_market_outcome FROM calibration_observations WHERE status = 'resolved')",
            fetch_all=True,
            as_dict=True,
        )
        await system.calibration_observation_service.export_csv_artifacts()

        with open(tmp_path / "calibration_dataset_v2.csv", newline="", encoding="utf-8") as dataset_file:
            exported_rows = list(csv.DictReader(dataset_file))

        assert len(rows) == 1
        assert rows[0]["schema_version"] == "2"
        assert rows[0]["actual_yes_outcome"] == "1"
        assert rows[0]["eventual_yes_market_outcome"] == "1"
        assert exported_rows[0]["feature_space"] == "yes_side_raw_probability"
        assert exported_rows[0]["yes_side_raw_probability"] == exported_rows[0]["raw_yes_prob"]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_seeded_quarantine_blocks_market_after_observation(tmp_path, base_config):
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **(base_config["runtime_controls"] | {"blocked_markets": ["quarantine-market"]}),
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "observation_export_path": str(tmp_path / "calibration_observations.csv"),
            },
            "quarantine": {
                **base_config["runtime_controls"]["quarantine"],
            },
        },
    })
    system = TradingSystem(config)
    system.execution = AsyncMock()
    ledger = await _attach_sqlite_quarantine_store(system, tmp_path / "quarantine.db")
    system.charlie_gate = CaptureCharlieGate(result=DummyRec())

    try:
        await system._execute_opportunity(
            {
                "market_id": "quarantine-market",
                "token_id": "token-yes",
                "side": "YES",
                "edge": Decimal("0.05"),
                "market_price": Decimal("0.40"),
                "confidence": "HIGH",
                "question": "Will BTC settle up?",
            },
            trigger="test",
        )

        rows = await ledger.execute(
            "SELECT guard_block_reason FROM calibration_observations",
            fetch_all=True,
            as_dict=True,
        )
        await system.calibration_observation_service.export_csv_artifacts(observations=True, dataset=False)

        with open(tmp_path / "calibration_observations.csv", newline="", encoding="utf-8") as observation_file:
            exported_rows = list(csv.DictReader(observation_file))

        assert len(system.charlie_gate.calls) == 1
        assert len(rows) == 1
        assert rows[0]["guard_block_reason"] == "quarantine"
        assert exported_rows[0]["guard_block_reason"] == "quarantine"
        assert system._session_stats["blocked_quarantine"] == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_candidate_snapshot_schema_migration_adds_meta_columns(tmp_path):
    ledger = AsyncLedger(db_path=str(tmp_path / "schema_migration.db"))
    await ledger.initialize()
    try:
        columns = await ledger.execute(
            "PRAGMA table_info(calibration_observations)",
            fetch_all=True,
        )
        column_names = {row[1] for row in columns}
        assert "candidate_id" in column_names
        assert "cluster_id" in column_names
        assert "feature_snapshot_ts" in column_names
        assert "feature_schema_version" in column_names
        assert "cluster_policy_version" in column_names
        assert "training_eligibility" in column_names
        assert "charlie_confidence" in column_names
        assert "charlie_implied_prob" in column_names
        assert "charlie_edge" in column_names
        assert "spread_bps" in column_names
        assert "time_to_expiry_seconds" in column_names
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_candidate_snapshot_persists_meta_ready_fields(tmp_path, base_config):
    end_date = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "observation_export_path": str(tmp_path / "candidate_observations.csv"),
                "dataset_export_path": str(tmp_path / "candidate_dataset.csv"),
                "meta_candidate_feature_schema_version": "meta_candidate_v1_test",
                "meta_candidate_cluster_policy_version": "cluster_v1_test",
                "meta_candidate_cluster_time_bucket_seconds": 10,
                "meta_candidate_cluster_price_bucket_abs": "0.01",
            },
        },
    })
    system = TradingSystem(config)
    system.execution = AsyncMock()
    system.execution.place_order_with_risk_check = AsyncMock()
    system.charlie_gate = CaptureCharlieGate(result=DummyRec(side="NO"))
    system._calibration_guard_status = {
        "blocked": True,
        "reason": "non_positive_coef=-0.1",
        "coef": -0.1,
        "monotonic": False,
    }
    ledger = await _attach_sqlite_runtime_store(system, tmp_path / "candidate_fields.db")

    try:
        await system._execute_opportunity(
            {
                "market_id": "market-candidate",
                "token_id": "token-no",
                "side": "NO",
                "edge": Decimal("0.05"),
                "market_price": Decimal("0.37"),
                "confidence": "HIGH",
                "question": "Will BTC settle up?",
                "spread_bps": Decimal("2233.33"),
                "endDate": end_date,
            },
            trigger="price_tick",
        )

        rows = await ledger.execute(
            """
            SELECT observation_id, candidate_id, cluster_id, feature_snapshot_ts,
                   feature_schema_version, cluster_policy_version, training_eligibility,
                   charlie_confidence, charlie_implied_prob, charlie_edge,
                   spread_bps, time_to_expiry_seconds, timestamp, observed_at,
                   actual_yes_outcome, resolved_at
            FROM calibration_observations
            """,
            fetch_all=True,
            as_dict=True,
        )
        assert len(rows) == 1
        row = rows[0]

        assert row["candidate_id"] == row["observation_id"]
        assert row["feature_snapshot_ts"] == row["observed_at"]
        assert row["feature_snapshot_ts"] == row["timestamp"]
        assert str(row["feature_snapshot_ts"]).endswith("Z")
        assert row["feature_schema_version"] == "meta_candidate_v1_test"
        assert row["cluster_policy_version"] == "cluster_v1_test"
        assert row["training_eligibility"] == "blocked_pre_execution"
        assert row["charlie_confidence"] == "0.8"
        assert row["charlie_implied_prob"] == "0.5"
        assert row["charlie_edge"] == "0.08"
        assert row["spread_bps"] == "2233.33"
        assert 0 < int(row["time_to_expiry_seconds"]) <= 3 * 60 * 60
        assert row["actual_yes_outcome"] == ""
        assert row["resolved_at"] == ""

        expected_cluster_id = system.calibration_observation_service.compute_cluster_id(
            market_id="market-candidate",
            selected_side="NO",
            trigger="price_tick",
            feature_snapshot_ts=row["feature_snapshot_ts"],
            token_price=Decimal("0.37"),
        )
        assert row["cluster_id"] == expected_cluster_id
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_feature_snapshot_ts_is_immutable_after_observation_updates(tmp_path, base_config):
    config = _resolve_runtime_controls(base_config | {
        "runtime_controls": {
            **base_config["runtime_controls"],
            "calibration": {
                **base_config["runtime_controls"]["calibration"],
                "observation_export_path": str(tmp_path / "snapshot_observations.csv"),
                "dataset_export_path": str(tmp_path / "snapshot_dataset.csv"),
            },
        },
    })
    system = TradingSystem(config)
    ledger = await _attach_sqlite_runtime_store(system, tmp_path / "snapshot_immutable.db")

    try:
        observation_id = await system._record_calibration_observation(
            market_id="market-snapshot",
            token_id="token-yes",
            opportunity={
                "side": "YES",
                "question": "Will BTC settle up?",
                "endDate": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
            charlie_rec=DummyRec(side="YES"),
            token_price=Decimal("0.42"),
            normalized_yes_price=Decimal("0.42"),
            trigger="immutability_test",
            observation_mode="trade_enabled",
        )

        original_row = await ledger.execute(
            "SELECT feature_snapshot_ts, updated_at FROM calibration_observations WHERE observation_id = ?",
            (observation_id,),
            fetch_one=True,
            as_dict=True,
        )
        await system._update_calibration_observation(
            observation_id,
            training_eligibility="pending_resolution",
            order_id="order-123",
            status="resolved",
            resolved_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        updated_row = await ledger.execute(
            "SELECT feature_snapshot_ts, updated_at, training_eligibility, order_id, status FROM calibration_observations WHERE observation_id = ?",
            (observation_id,),
            fetch_one=True,
            as_dict=True,
        )

        assert original_row["feature_snapshot_ts"] == updated_row["feature_snapshot_ts"]
        assert original_row["updated_at"] != updated_row["updated_at"]
        assert updated_row["training_eligibility"] == "pending_resolution"
        assert updated_row["order_id"] == "order-123"
        assert updated_row["status"] == "resolved"
    finally:
        await ledger.close()
