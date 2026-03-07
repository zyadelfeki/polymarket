from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from database.ledger_async import AsyncLedger
from services.calibration_observation_service import (
    META_SPLIT_MANIFEST_FIELDNAMES,
    CalibrationObservationService,
)


async def _make_service(tmp_path):
    ledger = AsyncLedger(db_path=str(tmp_path / "meta_materializer.db"))
    await ledger.initialize()
    service = CalibrationObservationService(
        ledger=ledger,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    return ledger, service


async def _insert_observation(
    ledger: AsyncLedger,
    *,
    observation_id: str,
    candidate_id: str,
    cluster_id: str,
    feature_snapshot_ts: str,
    market_id: str,
    training_eligibility: str,
    status: str = "pending",
    order_id: str = "",
    actual_yes_outcome: str = "",
    eventual_yes_market_outcome: str = "",
    guard_block_reason: str = "",
    selected_side: str = "YES",
    token_price: str = "0.40",
    normalized_yes_price: str = "0.40",
) -> None:
    await ledger.execute(
        """
        INSERT INTO calibration_observations (
            observation_id, candidate_id, cluster_id, feature_snapshot_ts,
            feature_schema_version, cluster_policy_version, training_eligibility,
            market_id, token_id, market_question,
            signal_side, opportunity_side, selected_side,
            observation_source, observation_mode,
            raw_yes_prob, yes_side_raw_probability, calibrated_yes_prob, selected_side_prob,
            charlie_confidence, charlie_implied_prob, charlie_edge, spread_bps, time_to_expiry_seconds,
            token_price, normalized_yes_price,
            timestamp, observed_at, resolution_time_hint,
            guard_block_reason, calibration_blocked, trigger,
            status, actual_yes_outcome, eventual_yes_market_outcome,
            resolved_at, resolution_time, order_id, trade_outcome,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            candidate_id,
            cluster_id,
            feature_snapshot_ts,
            "meta_candidate_v1",
            "cluster_v1",
            training_eligibility,
            market_id,
            "token-yes",
            f"Question {market_id}",
            selected_side,
            selected_side,
            selected_side,
            "charlie_scored_opportunity",
            "trade_enabled",
            "0.64",
            "0.64",
            "0.62",
            "0.62",
            "0.8",
            "0.5",
            "0.08",
            "120.0",
            3600,
            token_price,
            normalized_yes_price,
            feature_snapshot_ts,
            feature_snapshot_ts,
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            guard_block_reason,
            0,
            "unit_test",
            status,
            actual_yes_outcome,
            eventual_yes_market_outcome,
            feature_snapshot_ts if status == "resolved" else "",
            feature_snapshot_ts if status == "resolved" else "",
            order_id,
            "",
            feature_snapshot_ts,
            feature_snapshot_ts,
        ),
        commit=True,
    )


async def _insert_order(
    ledger: AsyncLedger,
    *,
    order_id: str,
    market_id: str,
    order_state: str,
    size: str,
    price: str,
    pnl: str | None,
    opened_at: str,
    closed_at: str | None,
) -> None:
    await ledger.execute(
        """
        INSERT INTO order_tracking (
            order_id, market_id, token_id, outcome, side, size, price,
            order_state, opened_at, closed_at, pnl,
            charlie_p_win, charlie_conf, charlie_regime, strategy, model_votes, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            market_id,
            "token-yes",
            "YES",
            "BUY",
            size,
            price,
            order_state,
            opened_at,
            closed_at,
            pnl,
            "0.62",
            "0.8",
            "BULLISH",
            "latency_arbitrage_btc",
            None,
            None,
        ),
        commit=True,
    )


async def _insert_idempotency(
    ledger: AsyncLedger,
    *,
    idempotency_key: str,
    order_id: str,
    status: str,
    filled_quantity: str,
    filled_price: str,
    fees: str = "0",
) -> None:
    await ledger.execute(
        """
        INSERT INTO idempotency_log (
            idempotency_key, order_id, correlation_id, status,
            filled_quantity, filled_price, fees, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            idempotency_key,
            order_id,
            f"corr-{order_id}",
            status,
            filled_quantity,
            filled_price,
            fees,
        ),
        commit=True,
    )


@pytest.mark.asyncio
async def test_materializer_dedup_correctness_and_cluster_collapse(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        t0 = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc)
        await _insert_observation(
            ledger,
            observation_id="obs-1",
            candidate_id="cand-1",
            cluster_id="cluster-a",
            feature_snapshot_ts=t0.isoformat().replace("+00:00", "Z"),
            market_id="market-a",
            training_eligibility="blocked_pre_execution",
        )
        await _insert_observation(
            ledger,
            observation_id="obs-2",
            candidate_id="cand-2",
            cluster_id="cluster-a",
            feature_snapshot_ts=(t0 + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
            market_id="market-a-dup",
            training_eligibility="blocked_pre_execution",
        )
        await _insert_observation(
            ledger,
            observation_id="obs-3",
            candidate_id="cand-3",
            cluster_id="cluster-b",
            feature_snapshot_ts=(t0 + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            market_id="market-b",
            training_eligibility="blocked_pre_execution",
        )

        report = await service.build_meta_materialization()

        assert [row["candidate_id"] for row in report["candidate_exhaust"]] == ["cand-1", "cand-3"]
        assert report["candidate_exhaust"][0]["cluster_candidate_count"] == 2
        assert report["dropped_candidate_rows"] == [
            {
                "candidate_id": "cand-2",
                "observation_id": "obs-2",
                "cluster_id": "cluster-a",
                "drop_reason": "cluster_duplicate",
                "kept_candidate_id": "cand-1",
            }
        ]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_materializer_candidate_exhaust_ordering_integrity(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        base = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc)
        await _insert_observation(
            ledger,
            observation_id="obs-z",
            candidate_id="cand-z",
            cluster_id="cluster-z",
            feature_snapshot_ts=(base + timedelta(seconds=10)).isoformat().replace("+00:00", "Z"),
            market_id="market-z",
            training_eligibility="blocked_pre_execution",
        )
        await _insert_observation(
            ledger,
            observation_id="obs-a",
            candidate_id="cand-a",
            cluster_id="cluster-a",
            feature_snapshot_ts=base.isoformat().replace("+00:00", "Z"),
            market_id="market-a",
            training_eligibility="blocked_pre_execution",
        )

        report = await service.build_meta_materialization()
        assert [row["candidate_id"] for row in report["candidate_exhaust"]] == ["cand-a", "cand-z"]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_materializer_label_correctness(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        snapshot_ts = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        await _insert_observation(
            ledger,
            observation_id="obs-profit",
            candidate_id="cand-profit",
            cluster_id="cluster-profit",
            feature_snapshot_ts=snapshot_ts,
            market_id="market-profit",
            training_eligibility="pending_resolution",
            status="resolved",
            order_id="ord-profit",
            actual_yes_outcome="1",
            eventual_yes_market_outcome="1",
        )
        await _insert_order(
            ledger,
            order_id="ord-profit",
            market_id="market-profit",
            order_state="SETTLED",
            size="100",
            price="0.50",
            pnl="2.00",
            opened_at=snapshot_ts,
            closed_at=(datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")),
        )
        await _insert_idempotency(
            ledger,
            idempotency_key="idem-profit",
            order_id="ord-profit",
            status="filled",
            filled_quantity="200",
            filled_price="0.50",
        )

        report = await service.build_meta_materialization(min_positive_return_bps="100", min_fill_ratio="1.0")
        rows = report["executed_profitability"]
        assert len(rows) == 1
        assert rows[0]["candidate_id"] == "cand-profit"
        assert rows[0]["profitability_label"] == 1
        assert rows[0]["realized_return_bps"] == "200.000000"
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_materializer_unresolved_and_canceled_rows_are_dropped_with_reasons(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        snapshot_ts = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        await _insert_observation(
            ledger,
            observation_id="obs-unresolved",
            candidate_id="cand-unresolved",
            cluster_id="cluster-unresolved",
            feature_snapshot_ts=snapshot_ts,
            market_id="market-unresolved",
            training_eligibility="pending_resolution",
            status="pending",
            order_id="ord-unresolved",
        )
        await _insert_order(
            ledger,
            order_id="ord-unresolved",
            market_id="market-unresolved",
            order_state="SUBMITTED",
            size="100",
            price="0.50",
            pnl=None,
            opened_at=snapshot_ts,
            closed_at=None,
        )
        await _insert_observation(
            ledger,
            observation_id="obs-cancelled",
            candidate_id="cand-cancelled",
            cluster_id="cluster-cancelled",
            feature_snapshot_ts=(datetime(2026, 3, 7, 0, 5, tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z"),
            market_id="market-cancelled",
            training_eligibility="pending_resolution",
            status="resolved",
            order_id="ord-cancelled",
            actual_yes_outcome="0",
            eventual_yes_market_outcome="0",
        )
        await _insert_order(
            ledger,
            order_id="ord-cancelled",
            market_id="market-cancelled",
            order_state="CANCELLED",
            size="100",
            price="0.40",
            pnl=None,
            opened_at=snapshot_ts,
            closed_at=(datetime(2026, 3, 7, 0, 10, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")),
        )

        report = await service.build_meta_materialization()
        dropped = report["dropped_executed_rows"]
        assert {item["candidate_id"]: item["drop_reason"] for item in dropped} == {
            "cand-unresolved": "unresolved_observation",
            "cand-cancelled": "order_state:cancelled",
        }
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_materializer_partial_fill_exclusion(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        snapshot_ts = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        await _insert_observation(
            ledger,
            observation_id="obs-partial",
            candidate_id="cand-partial",
            cluster_id="cluster-partial",
            feature_snapshot_ts=snapshot_ts,
            market_id="market-partial",
            training_eligibility="pending_resolution",
            status="resolved",
            order_id="ord-partial",
            actual_yes_outcome="1",
            eventual_yes_market_outcome="1",
        )
        await _insert_order(
            ledger,
            order_id="ord-partial",
            market_id="market-partial",
            order_state="SETTLED",
            size="100",
            price="0.50",
            pnl="1.00",
            opened_at=snapshot_ts,
            closed_at=(datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")),
        )
        await _insert_idempotency(
            ledger,
            idempotency_key="idem-partial",
            order_id="ord-partial",
            status="partially_filled",
            filled_quantity="100",
            filled_price="0.50",
        )

        report = await service.build_meta_materialization(min_fill_ratio="1.0")
        assert report["executed_profitability"] == []
        assert report["dropped_executed_rows"] == [
            {
                "candidate_id": "cand-partial",
                "observation_id": "obs-partial",
                "cluster_id": "cluster-partial",
                "order_id": "ord-partial",
                "drop_reason": "partial_fill_excluded",
                "fill_ratio": "0.500000",
            }
        ]
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_materializer_selects_latest_idempotency_row_once_per_order_and_is_stable(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        snapshot_ts = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        await _insert_observation(
            ledger,
            observation_id="obs-latest",
            candidate_id="cand-latest",
            cluster_id="cluster-latest",
            feature_snapshot_ts=snapshot_ts,
            market_id="market-latest",
            training_eligibility="pending_resolution",
            status="resolved",
            order_id="ord-latest",
            actual_yes_outcome="1",
            eventual_yes_market_outcome="1",
        )
        await _insert_order(
            ledger,
            order_id="ord-latest",
            market_id="market-latest",
            order_state="SETTLED",
            size="100",
            price="0.50",
            pnl="1.50",
            opened_at=snapshot_ts,
            closed_at=(datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")),
        )
        await ledger.execute(
            """
            INSERT INTO idempotency_log (
                idempotency_key, order_id, correlation_id, status,
                filled_quantity, filled_price, fees, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "idem-old",
                "ord-latest",
                "corr-ord-latest-old",
                "filled",
                "100",
                "0.49",
                "0",
                "2026-03-07T00:10:00Z",
                "2026-03-07T00:10:00Z",
            ),
            commit=True,
        )
        await ledger.execute(
            """
            INSERT INTO idempotency_log (
                idempotency_key, order_id, correlation_id, status,
                filled_quantity, filled_price, fees, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "idem-new",
                "ord-latest",
                "corr-ord-latest-new",
                "filled",
                "200",
                "0.50",
                "0",
                "2026-03-07T00:20:00Z",
                "2026-03-07T00:20:00Z",
            ),
            commit=True,
        )

        first_report = await service.build_meta_materialization(min_fill_ratio="1.0")
        second_report = await service.build_meta_materialization(min_fill_ratio="1.0")

        assert len(first_report["executed_profitability"]) == 1
        assert len(second_report["executed_profitability"]) == 1
        assert first_report["executed_profitability"] == second_report["executed_profitability"]
        assert first_report["executed_profitability"][0]["candidate_id"] == "cand-latest"
        assert first_report["executed_profitability"][0]["filled_quantity"] == "200.00000000"
        assert first_report["executed_profitability"][0]["filled_price"] == "0.50000000"
        assert first_report["executed_profitability"][0]["fill_ratio"] == "1.000000"

        source_rows = await service._fetch_meta_materialization_source_rows()
        latest_row = next(row for row in source_rows if row.get("order_id") == "ord-latest")
        assert latest_row["idempotency_key"] == "idem-new"
        assert str(latest_row["filled_quantity"]) == "200"
        assert str(latest_row["filled_price"]) == "0.5"
        assert latest_row["idempotency_updated_at"] == "2026-03-07T00:20:00Z"
    finally:
        await ledger.close()


def _executed_row(candidate_id: str, cluster_id: str, feature_snapshot_ts: str, **overrides):
    row = {
        "candidate_id": candidate_id,
        "observation_id": f"obs-{candidate_id}",
        "cluster_id": cluster_id,
        "feature_snapshot_ts": feature_snapshot_ts,
        "feature_schema_version": "meta_candidate_v1",
        "cluster_policy_version": "cluster_v1",
        "market_id": f"market-{candidate_id}",
        "token_id": "token-yes",
        "market_question": f"Question {candidate_id}",
        "selected_side": "YES",
        "order_id": f"order-{candidate_id}",
        "order_state": "SETTLED",
        "order_opened_at": feature_snapshot_ts,
        "order_closed_at": feature_snapshot_ts,
        "requested_notional": "100.00000000",
        "requested_quantity": "200.00000000",
        "filled_quantity": "200.00000000",
        "filled_price": "0.50000000",
        "fill_ratio": "1.000000",
        "min_fill_ratio": "1.0",
        "min_positive_return_bps": "0",
        "settled_pnl": "2.00000000",
        "realized_return_bps": "200.000000",
        "profitability_label": 1,
        "actual_yes_outcome": "1",
        "eventual_yes_market_outcome": "1",
        "training_eligibility": "pending_resolution",
        "raw_yes_prob": "0.64",
        "yes_side_raw_probability": "0.64",
        "calibrated_yes_prob": "0.62",
        "selected_side_prob": "0.62",
        "charlie_confidence": "0.8",
        "charlie_implied_prob": "0.5",
        "charlie_edge": "0.08",
        "spread_bps": "120.0",
        "time_to_expiry_seconds": "3600",
        "token_price": "0.40",
        "normalized_yes_price": "0.40",
    }
    row.update(overrides)
    return row


def test_duplicate_candidate_rejection_in_training_input(tmp_path):
    service = CalibrationObservationService(
        ledger=None,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    duplicate_rows = [
        _executed_row("cand-1", "cluster-1", "2026-03-07T00:00:00Z"),
        _executed_row("cand-1", "cluster-2", "2026-03-07T00:00:10Z"),
    ]

    with pytest.raises(ValueError, match="duplicate candidate_id"):
        service.build_training_split_manifest(duplicate_rows)


def test_cluster_exclusivity_enforced_across_splits(tmp_path):
    service = CalibrationObservationService(
        ledger=None,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    rows = [
        _executed_row(f"cand-{index}", f"cluster-{index}", f"2026-03-07T00:00:{index:02d}Z")
        for index in range(10)
    ]

    manifest = service.build_training_split_manifest(rows)
    train_clusters = set(manifest["train_cluster_ids_ref"]["cluster_ids"])
    validation_clusters = set(manifest["validation_cluster_ids_ref"]["cluster_ids"])
    test_clusters = set(manifest["test_cluster_ids_ref"]["cluster_ids"])

    assert train_clusters.isdisjoint(validation_clusters)
    assert train_clusters.isdisjoint(test_clusters)
    assert validation_clusters.isdisjoint(test_clusters)


def test_deterministic_split_reproducibility(tmp_path):
    service = CalibrationObservationService(
        ledger=None,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    rows = [
        _executed_row("cand-b", "cluster-b", "2026-03-07T00:00:10Z"),
        _executed_row("cand-a", "cluster-a", "2026-03-07T00:00:00Z"),
        _executed_row("cand-c", "cluster-c", "2026-03-07T00:00:20Z"),
        _executed_row("cand-d", "cluster-d", "2026-03-07T00:00:30Z"),
        _executed_row("cand-e", "cluster-e", "2026-03-07T00:00:40Z"),
    ]

    first_manifest = service.build_training_split_manifest(rows)
    second_manifest = service.build_training_split_manifest(list(reversed(rows)))

    assert first_manifest["split_policy_hash"] == second_manifest["split_policy_hash"]
    assert first_manifest["train_cluster_ids_ref"] == second_manifest["train_cluster_ids_ref"]
    assert first_manifest["validation_cluster_ids_ref"] == second_manifest["validation_cluster_ids_ref"]
    assert first_manifest["test_cluster_ids_ref"] == second_manifest["test_cluster_ids_ref"]
    assert first_manifest["train_time_start"] == second_manifest["train_time_start"]
    assert first_manifest["test_time_end"] == second_manifest["test_time_end"]


def test_boundary_bucket_assignment_goes_to_earlier_split(tmp_path):
    service = CalibrationObservationService(
        ledger=None,
        observation_export_path=str(tmp_path / "observations.csv"),
        dataset_export_path=str(tmp_path / "dataset.csv"),
    )
    rows = [
        _executed_row("cand-1", "cluster-1", "2026-03-07T00:00:00Z"),
        _executed_row("cand-2", "cluster-2", "2026-03-07T00:00:01Z"),
        _executed_row("cand-3", "cluster-3", "2026-03-07T00:00:02Z"),
        _executed_row("cand-4", "cluster-4", "2026-03-07T00:00:03Z"),
        _executed_row("cand-5", "cluster-5", "2026-03-07T00:00:04Z"),
        _executed_row("cand-6", "cluster-6", "2026-03-07T00:00:05Z"),
        _executed_row("cand-7", "cluster-7", "2026-03-07T00:00:06Z"),
        _executed_row("cand-8", "cluster-8", "2026-03-07T00:00:07Z"),
        _executed_row("cand-9", "cluster-9", "2026-03-07T00:00:07Z"),
        _executed_row("cand-10", "cluster-10", "2026-03-07T00:00:08Z"),
    ]

    manifest = service.build_training_split_manifest(rows)

    assert manifest["target_train_row_count"] == 7
    assert manifest["train_row_count"] == 7
    assert manifest["validation_row_count"] == 2
    assert manifest["test_row_count"] == 1
    assert manifest["train_time_end"] == "2026-03-07T00:00:06Z"
    assert manifest["boundary_bucket_assignments"] == [
        {
            "split": "validation",
            "feature_snapshot_ts": "2026-03-07T00:00:07Z",
            "bucket_row_count": 2,
            "target_row_count": 1,
            "actual_row_count_after_assignment": 2,
        },
    ]


@pytest.mark.asyncio
async def test_split_manifest_schema_validation_and_artifact_generation(tmp_path):
    ledger, service = await _make_service(tmp_path)
    try:
        base = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc)
        for index in range(8):
            snapshot_ts = (base + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
            await _insert_observation(
                ledger,
                observation_id=f"obs-{index}",
                candidate_id=f"cand-{index}",
                cluster_id=f"cluster-{index}",
                feature_snapshot_ts=snapshot_ts,
                market_id=f"market-{index}",
                training_eligibility="pending_resolution",
                status="resolved",
                order_id=f"ord-{index}",
                actual_yes_outcome="1",
                eventual_yes_market_outcome="1",
            )
            await _insert_order(
                ledger,
                order_id=f"ord-{index}",
                market_id=f"market-{index}",
                order_state="SETTLED",
                size="100",
                price="0.50",
                pnl="2.00",
                opened_at=snapshot_ts,
                closed_at=snapshot_ts,
            )
            await _insert_idempotency(
                ledger,
                idempotency_key=f"idem-{index}",
                order_id=f"ord-{index}",
                status="filled",
                filled_quantity="200",
                filled_price="0.50",
            )

        report = await service.materialize_meta_datasets(
            candidate_exhaust_path=str(tmp_path / "candidate_exhaust.csv"),
            executed_profitability_path=str(tmp_path / "executed_profitability.csv"),
            split_manifest_path=str(tmp_path / "split_manifest.json"),
        )

        with open(tmp_path / "split_manifest.json", "r", encoding="utf-8") as split_manifest_file:
            manifest = json.load(split_manifest_file)

        assert set(META_SPLIT_MANIFEST_FIELDNAMES).issubset(manifest.keys())
        assert manifest["split_policy_version"] == "chronological_cluster_no_purge_v1"
        assert manifest["purge_policy"] == "none_v1"
        assert manifest["sort_key"] == "feature_snapshot_ts,candidate_id"
        assert manifest["train_cluster_ids_ref"]["storage"] == "embedded"
        assert report["split_manifest_path"] == str(tmp_path / "split_manifest.json")
    finally:
        await ledger.close()
