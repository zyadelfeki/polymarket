import sqlite3

import pytest

from database.ledger_async import AsyncLedger


LEGACY_CALIBRATION_OBSERVATIONS_SQL = """
CREATE TABLE calibration_observations (
    observation_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL DEFAULT '',
    market_question TEXT,
    signal_side TEXT,
    opportunity_side TEXT,
    selected_side TEXT,
    observation_source TEXT NOT NULL,
    observation_mode TEXT NOT NULL,
    raw_yes_prob TEXT,
    yes_side_raw_probability TEXT,
    calibrated_yes_prob TEXT,
    selected_side_prob TEXT,
    token_price TEXT,
    normalized_yes_price TEXT,
    timestamp TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    resolution_time_hint TEXT,
    guard_block_reason TEXT,
    calibration_blocked INTEGER NOT NULL DEFAULT 0,
    trigger TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    actual_yes_outcome TEXT,
    eventual_yes_market_outcome TEXT,
    resolved_at TEXT,
    resolution_time TEXT,
    order_id TEXT,
    trade_outcome TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _create_legacy_db(db_path, *, with_schema_version_v6: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(LEGACY_CALIBRATION_OBSERVATIONS_SQL)
        conn.execute(
            """
            INSERT INTO calibration_observations (
                observation_id,
                market_id,
                token_id,
                market_question,
                signal_side,
                opportunity_side,
                selected_side,
                observation_source,
                observation_mode,
                raw_yes_prob,
                yes_side_raw_probability,
                calibrated_yes_prob,
                selected_side_prob,
                token_price,
                normalized_yes_price,
                timestamp,
                observed_at,
                resolution_time_hint,
                guard_block_reason,
                calibration_blocked,
                trigger,
                status,
                actual_yes_outcome,
                eventual_yes_market_outcome,
                resolved_at,
                resolution_time,
                order_id,
                trade_outcome,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "obs-1",
                "market-1",
                "token-1",
                "Question 1",
                "YES",
                "YES",
                "YES",
                "charlie_scored_opportunity",
                "paper",
                "0.62",
                "0.62",
                "0.61",
                "0.61",
                "0.44",
                "0.44",
                "2026-03-07T00:00:00Z",
                "2026-03-07T00:00:00Z",
                "2026-03-07T01:00:00Z",
                "",
                0,
                "unit_test",
                "pending",
                "",
                "",
                "",
                "",
                "",
                "",
                "2026-03-07T00:00:00Z",
                "2026-03-07T00:00:00Z",
            ),
        )
        if with_schema_version_v6:
            conn.execute(
                """
                CREATE TABLE schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    description TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_version (version, description) VALUES (?, ?)",
                (6, "candidate snapshot metadata for meta-ready observation exhaust"),
            )
        conn.commit()
    finally:
        conn.close()


def _read_columns(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[1]
            for row in conn.execute("PRAGMA table_info(calibration_observations)").fetchall()
        }
    finally:
        conn.close()


def _read_index_names(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            row[1]
            for row in conn.execute("PRAGMA index_list(calibration_observations)").fetchall()
        }
    finally:
        conn.close()


def _read_schema_version_count(db_path, version: int) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?",
            (version,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _read_observation_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT observation_id, candidate_id, cluster_id, feature_snapshot_ts
            FROM calibration_observations
            WHERE observation_id = ?
            """,
            ("obs-1",),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_legacy_db_migration_succeeds(tmp_path):
    db_path = tmp_path / "legacy_schema.db"
    _create_legacy_db(str(db_path))

    ledger = AsyncLedger(db_path=str(db_path))
    try:
        await ledger.initialize()
    finally:
        await ledger.close()

    columns = _read_columns(str(db_path))
    assert "candidate_id" in columns
    assert "cluster_id" in columns
    assert "feature_snapshot_ts" in columns
    assert "training_eligibility" in columns

    index_names = _read_index_names(str(db_path))
    assert "idx_calibration_observations_cluster" in index_names

    row = _read_observation_row(str(db_path))
    assert row["candidate_id"] == "obs-1"
    assert row["cluster_id"] == ""
    assert row["feature_snapshot_ts"] == "2026-03-07T00:00:00Z"

    assert _read_schema_version_count(str(db_path), 6) == 1


@pytest.mark.asyncio
async def test_second_initialization_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy_schema_idempotent.db"
    _create_legacy_db(str(db_path))

    first_ledger = AsyncLedger(db_path=str(db_path))
    try:
        await first_ledger.initialize()
    finally:
        await first_ledger.close()

    second_ledger = AsyncLedger(db_path=str(db_path))
    try:
        await second_ledger.initialize()
    finally:
        await second_ledger.close()

    columns = _read_columns(str(db_path))
    assert "candidate_id" in columns
    assert "cluster_id" in columns
    assert "feature_snapshot_ts" in columns

    index_names = _read_index_names(str(db_path))
    assert "idx_calibration_observations_cluster" in index_names
    assert _read_schema_version_count(str(db_path), 6) == 1

    row = _read_observation_row(str(db_path))
    assert row["candidate_id"] == "obs-1"
    assert row["feature_snapshot_ts"] == "2026-03-07T00:00:00Z"


@pytest.mark.asyncio
async def test_schema_version_does_not_bypass_physical_schema_verification(tmp_path):
    db_path = tmp_path / "legacy_schema_version_claim.db"
    _create_legacy_db(str(db_path), with_schema_version_v6=True)

    ledger = AsyncLedger(db_path=str(db_path))
    try:
        await ledger.initialize()
    finally:
        await ledger.close()

    columns = _read_columns(str(db_path))
    assert "candidate_id" in columns
    assert "cluster_id" in columns
    assert "feature_snapshot_ts" in columns
    assert "training_eligibility" in columns

    row = _read_observation_row(str(db_path))
    assert row["candidate_id"] == "obs-1"
    assert row["feature_snapshot_ts"] == "2026-03-07T00:00:00Z"
    assert _read_schema_version_count(str(db_path), 6) == 1