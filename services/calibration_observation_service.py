from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logger = logging.getLogger(__name__)


DEFAULT_META_FEATURE_SCHEMA_VERSION = "meta_candidate_v1"
DEFAULT_META_CLUSTER_POLICY_VERSION = "cluster_v1"
DEFAULT_META_CLUSTER_TIME_BUCKET_SECONDS = 10
DEFAULT_META_CLUSTER_PRICE_BUCKET_ABS = Decimal("0.01")


CALIBRATION_DATASET_FIELDNAMES = [
    "schema_version",
    "feature_space",
    "label_space",
    "market_id",
    "observation_id",
    "order_id",
    "signal_side",
    "trade_side",
    "selected_side",
    "observation_source",
    "observation_mode",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "actual_yes_outcome",
    "eventual_yes_market_outcome",
    "trade_outcome",
    "token_price",
    "normalized_yes_price",
    "timestamp",
    "entry_time",
    "resolution_time",
    "guard_block_reason",
]

CALIBRATION_OBSERVATION_FIELDNAMES = [
    "observation_id",
    "candidate_id",
    "cluster_id",
    "feature_snapshot_ts",
    "feature_schema_version",
    "cluster_policy_version",
    "training_eligibility",
    "market_id",
    "token_id",
    "market_question",
    "signal_side",
    "opportunity_side",
    "selected_side",
    "observation_source",
    "observation_mode",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "charlie_confidence",
    "charlie_implied_prob",
    "charlie_edge",
    "spread_bps",
    "time_to_expiry_seconds",
    "token_price",
    "normalized_yes_price",
    "timestamp",
    "observed_at",
    "resolution_time_hint",
    "guard_block_reason",
    "calibration_blocked",
    "trigger",
    "status",
    "actual_yes_outcome",
    "eventual_yes_market_outcome",
    "resolved_at",
]

META_CANDIDATE_EXHAUST_FIELDNAMES = [
    "candidate_id",
    "observation_id",
    "cluster_id",
    "cluster_candidate_count",
    "feature_snapshot_ts",
    "feature_schema_version",
    "cluster_policy_version",
    "market_id",
    "token_id",
    "market_question",
    "signal_side",
    "opportunity_side",
    "selected_side",
    "observation_source",
    "observation_mode",
    "trigger",
    "training_eligibility",
    "guard_block_reason",
    "status",
    "order_id",
    "actual_yes_outcome",
    "eventual_yes_market_outcome",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "charlie_confidence",
    "charlie_implied_prob",
    "charlie_edge",
    "spread_bps",
    "time_to_expiry_seconds",
    "token_price",
    "normalized_yes_price",
    "timestamp",
    "observed_at",
    "resolved_at",
]

META_EXECUTED_PROFITABILITY_FIELDNAMES = [
    "candidate_id",
    "observation_id",
    "cluster_id",
    "feature_snapshot_ts",
    "feature_schema_version",
    "cluster_policy_version",
    "market_id",
    "token_id",
    "market_question",
    "selected_side",
    "order_id",
    "order_state",
    "order_opened_at",
    "order_closed_at",
    "requested_notional",
    "requested_quantity",
    "filled_quantity",
    "filled_price",
    "fill_ratio",
    "min_fill_ratio",
    "min_positive_return_bps",
    "settled_pnl",
    "realized_return_bps",
    "profitability_label",
    "actual_yes_outcome",
    "eventual_yes_market_outcome",
    "training_eligibility",
    "raw_yes_prob",
    "yes_side_raw_probability",
    "calibrated_yes_prob",
    "selected_side_prob",
    "charlie_confidence",
    "charlie_implied_prob",
    "charlie_edge",
    "spread_bps",
    "time_to_expiry_seconds",
    "token_price",
    "normalized_yes_price",
]

META_SPLIT_MANIFEST_FIELDNAMES = [
    "artifact_version",
    "run_id",
    "split_policy_version",
    "split_policy_hash",
    "feature_schema_version",
    "label_contract_version",
    "sort_key",
    "cluster_split_unit",
    "purge_policy",
    "train_row_count",
    "validation_row_count",
    "test_row_count",
    "train_cluster_count",
    "validation_cluster_count",
    "test_cluster_count",
    "train_time_start",
    "train_time_end",
    "validation_time_start",
    "validation_time_end",
    "test_time_start",
    "test_time_end",
    "train_cluster_ids_ref",
    "validation_cluster_ids_ref",
    "test_cluster_ids_ref",
    "created_at",
]

META_TRAINING_LABEL_CONTRACT_VERSION = "executed_profitability_v1"
META_TRAINING_SPLIT_POLICY_VERSION = "chronological_cluster_no_purge_v1"
META_TRAINING_SPLIT_ARTIFACT_VERSION = 1
META_TRAINING_SORT_KEY = "feature_snapshot_ts,candidate_id"
META_TRAINING_CLUSTER_SPLIT_UNIT = "cluster_id"
META_TRAINING_PURGE_POLICY = "none_v1"


class CalibrationObservationService:
    """Persists calibration observations in SQLite and exports CSV artifacts."""

    def __init__(
        self,
        *,
        ledger,
        observation_export_path: str,
        dataset_export_path: str,
        feature_schema_version: str = DEFAULT_META_FEATURE_SCHEMA_VERSION,
        cluster_policy_version: str = DEFAULT_META_CLUSTER_POLICY_VERSION,
        cluster_time_bucket_seconds: int = DEFAULT_META_CLUSTER_TIME_BUCKET_SECONDS,
        cluster_price_bucket_abs: Decimal | str = DEFAULT_META_CLUSTER_PRICE_BUCKET_ABS,
    ) -> None:
        self.ledger = ledger
        self.observation_export_path = Path(observation_export_path)
        self.dataset_export_path = Path(dataset_export_path)
        self.feature_schema_version = str(feature_schema_version or DEFAULT_META_FEATURE_SCHEMA_VERSION)
        self.cluster_policy_version = str(cluster_policy_version or DEFAULT_META_CLUSTER_POLICY_VERSION)
        self.cluster_time_bucket_seconds = max(1, int(cluster_time_bucket_seconds or DEFAULT_META_CLUSTER_TIME_BUCKET_SECONDS))
        self.cluster_price_bucket_abs = Decimal(str(cluster_price_bucket_abs or DEFAULT_META_CLUSTER_PRICE_BUCKET_ABS))

    @staticmethod
    def _log_event(event: str, **fields: Any) -> None:
        if _structlog_available:
            logger.info(event, **fields)
            return
        logger.info("%s %s", event, " ".join(f"{key}={value}" for key, value in fields.items()))

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _normalize_utc_text(cls, value: Any, *, default_now: bool = False) -> str:
        if value in {None, ""}:
            return cls._utc_now_iso() if default_now else ""
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        raw = str(value).strip()
        if not raw:
            return cls._utc_now_iso() if default_now else ""
        try:
            numeric_value = float(raw)
            return datetime.fromtimestamp(numeric_value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat().replace("+00:00", "Z")
        except ValueError:
            return raw

    @staticmethod
    def _bucket_price(value: Decimal, bucket_abs: Decimal) -> str:
        if bucket_abs <= Decimal("0"):
            return str(value)
        bucket_units = (value / bucket_abs).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return str((bucket_units * bucket_abs).normalize())

    @classmethod
    def _bucket_time_iso(cls, feature_snapshot_ts: str, bucket_seconds: int) -> str:
        normalized_snapshot_ts = cls._normalize_utc_text(feature_snapshot_ts, default_now=True)
        snapshot_dt = datetime.fromisoformat(normalized_snapshot_ts.replace("Z", "+00:00"))
        bucket_start = int(snapshot_dt.timestamp() // bucket_seconds) * bucket_seconds
        return datetime.fromtimestamp(bucket_start, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _time_to_expiry_seconds(cls, opportunity: Dict[str, Any], feature_snapshot_ts: str) -> Optional[int]:
        resolution_hint = (
            opportunity.get("end_time")
            or opportunity.get("endDate")
            or opportunity.get("endDateIso")
            or ""
        )
        normalized_hint = cls._normalize_utc_text(resolution_hint)
        if not normalized_hint:
            return None
        try:
            snapshot_dt = datetime.fromisoformat(feature_snapshot_ts.replace("Z", "+00:00"))
            resolution_dt = datetime.fromisoformat(normalized_hint.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int((resolution_dt - snapshot_dt).total_seconds())

    def compute_cluster_id(
        self,
        *,
        market_id: str,
        selected_side: str,
        trigger: str,
        feature_snapshot_ts: str,
        token_price: Decimal,
    ) -> str:
        cluster_material = "|".join(
            [
                str(market_id),
                str(selected_side),
                str(trigger),
                self._bucket_time_iso(feature_snapshot_ts, self.cluster_time_bucket_seconds),
                self._bucket_price(Decimal(str(token_price)), self.cluster_price_bucket_abs),
                self.cluster_policy_version,
            ]
        )
        return hashlib.sha256(cluster_material.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _to_decimal(value: Any, *, default: Optional[Decimal] = None) -> Optional[Decimal]:
        if value in {None, ""}:
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    @classmethod
    def _meta_sort_key(cls, row: Dict[str, Any]) -> tuple[str, str, str]:
        return (
            cls._normalize_utc_text(row.get("feature_snapshot_ts") or row.get("observed_at") or row.get("timestamp")),
            str(row.get("candidate_id") or row.get("observation_id") or ""),
            str(row.get("observation_id") or ""),
        )

    @classmethod
    def _training_sort_key(cls, row: Dict[str, Any]) -> Tuple[str, str]:
        return (
            cls._normalize_utc_text(row.get("feature_snapshot_ts")),
            str(row.get("candidate_id") or ""),
        )

    @classmethod
    def _build_split_policy_hash(cls) -> str:
        payload = {
            "split_policy_version": META_TRAINING_SPLIT_POLICY_VERSION,
            "sort_key": META_TRAINING_SORT_KEY,
            "cluster_split_unit": META_TRAINING_CLUSTER_SPLIT_UNIT,
            "purge_policy": META_TRAINING_PURGE_POLICY,
            "fractions": {
                "train": "0.70",
                "validation": "0.15",
                "test": "0.15",
            },
            "boundary_bucket_rule": "assign_entire_feature_snapshot_bucket_to_earlier_split",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _validate_training_input_rows(cls, executed_profitability_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        required_fields = {
            "candidate_id",
            "cluster_id",
            "feature_snapshot_ts",
            "feature_schema_version",
            "profitability_label",
        }
        normalized_rows: List[Dict[str, Any]] = []
        seen_candidate_ids: Dict[str, int] = {}
        seen_cluster_ids: Dict[str, int] = {}

        for index, raw_row in enumerate(executed_profitability_rows, start=1):
            row = dict(raw_row)
            missing_fields = []
            for field in sorted(required_fields):
                value = row.get(field)
                if value is None:
                    missing_fields.append(field)
                    continue
                if isinstance(value, str) and value.strip() == "":
                    missing_fields.append(field)
            if missing_fields:
                raise ValueError(
                    f"executed_profitability row {index} missing required fields: {', '.join(missing_fields)}"
                )

            candidate_id = str(row.get("candidate_id") or "").strip()
            cluster_id = str(row.get("cluster_id") or "").strip()
            feature_snapshot_ts = cls._normalize_utc_text(row.get("feature_snapshot_ts"))
            if not feature_snapshot_ts:
                raise ValueError(
                    f"executed_profitability row {index} has invalid feature_snapshot_ts"
                )

            if candidate_id in seen_candidate_ids:
                raise ValueError(
                    f"duplicate candidate_id detected in executed_profitability: {candidate_id}"
                )
            if cluster_id in seen_cluster_ids:
                raise ValueError(
                    f"duplicate cluster_id detected in executed_profitability: {cluster_id}"
                )

            seen_candidate_ids[candidate_id] = index
            seen_cluster_ids[cluster_id] = index
            row["candidate_id"] = candidate_id
            row["cluster_id"] = cluster_id
            row["feature_snapshot_ts"] = feature_snapshot_ts
            normalized_rows.append(row)

        return sorted(normalized_rows, key=cls._training_sort_key)

    @classmethod
    def build_training_split_manifest(
        cls,
        executed_profitability_rows: List[Dict[str, Any]],
        *,
        feature_schema_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_rows = cls._validate_training_input_rows(executed_profitability_rows)
        if not normalized_rows:
            raise ValueError("executed_profitability is empty; cannot build split manifest")

        split_policy_hash = cls._build_split_policy_hash()
        total_rows = len(normalized_rows)
        target_counts = {
            "train": int(total_rows * 0.70),
            "validation": int(total_rows * 0.15),
        }
        split_sequence = ["train", "validation", "test"]
        split_rows: Dict[str, List[Dict[str, Any]]] = {name: [] for name in split_sequence}
        bucketed_rows: Dict[str, List[Dict[str, Any]]] = {}
        for row in normalized_rows:
            bucketed_rows.setdefault(str(row["feature_snapshot_ts"]), []).append(row)

        current_split_index = 0
        boundary_bucket_assignments: List[Dict[str, Any]] = []
        for bucket_key in sorted(bucketed_rows.keys()):
            bucket_rows = bucketed_rows[bucket_key]
            split_name = split_sequence[current_split_index]
            before_count = len(split_rows[split_name])
            split_rows[split_name].extend(bucket_rows)

            if split_name != "test":
                target_count = target_counts[split_name]
                after_count = len(split_rows[split_name])
                if before_count < target_count < after_count:
                    boundary_bucket_assignments.append(
                        {
                            "split": split_name,
                            "feature_snapshot_ts": bucket_key,
                            "bucket_row_count": len(bucket_rows),
                            "target_row_count": target_count,
                            "actual_row_count_after_assignment": after_count,
                        }
                    )
                if after_count >= target_count:
                    current_split_index += 1

        def _cluster_ref(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
            cluster_ids = sorted({str(row["cluster_id"]) for row in rows})
            return {
                "storage": "embedded",
                "cluster_ids": cluster_ids,
            }

        def _time_boundary(rows: List[Dict[str, Any]]) -> Tuple[str, str]:
            if not rows:
                return "", ""
            return rows[0]["feature_snapshot_ts"], rows[-1]["feature_snapshot_ts"]

        train_time_start, train_time_end = _time_boundary(split_rows["train"])
        validation_time_start, validation_time_end = _time_boundary(split_rows["validation"])
        test_time_start, test_time_end = _time_boundary(split_rows["test"])
        manifest_feature_schema_version = str(
            feature_schema_version
            or normalized_rows[0].get("feature_schema_version")
            or DEFAULT_META_FEATURE_SCHEMA_VERSION
        )
        run_id = f"meta_split_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        manifest = {
            "artifact_version": META_TRAINING_SPLIT_ARTIFACT_VERSION,
            "run_id": run_id,
            "split_policy_version": META_TRAINING_SPLIT_POLICY_VERSION,
            "split_policy_hash": split_policy_hash,
            "feature_schema_version": manifest_feature_schema_version,
            "label_contract_version": META_TRAINING_LABEL_CONTRACT_VERSION,
            "sort_key": META_TRAINING_SORT_KEY,
            "cluster_split_unit": META_TRAINING_CLUSTER_SPLIT_UNIT,
            "purge_policy": META_TRAINING_PURGE_POLICY,
            "train_row_count": len(split_rows["train"]),
            "validation_row_count": len(split_rows["validation"]),
            "test_row_count": len(split_rows["test"]),
            "train_cluster_count": len(_cluster_ref(split_rows["train"])["cluster_ids"]),
            "validation_cluster_count": len(_cluster_ref(split_rows["validation"])["cluster_ids"]),
            "test_cluster_count": len(_cluster_ref(split_rows["test"])["cluster_ids"]),
            "train_time_start": train_time_start,
            "train_time_end": train_time_end,
            "validation_time_start": validation_time_start,
            "validation_time_end": validation_time_end,
            "test_time_start": test_time_start,
            "test_time_end": test_time_end,
            "train_cluster_ids_ref": _cluster_ref(split_rows["train"]),
            "validation_cluster_ids_ref": _cluster_ref(split_rows["validation"]),
            "test_cluster_ids_ref": _cluster_ref(split_rows["test"]),
            "created_at": cls._utc_now_iso(),
            "target_train_row_count": target_counts["train"],
            "target_validation_row_count": target_counts["validation"],
            "target_test_row_count": total_rows - target_counts["train"] - target_counts["validation"],
            "boundary_bucket_assignments": boundary_bucket_assignments,
            "split_targets": {
                "train": "0.70",
                "validation": "0.15",
                "test": "0.15",
            },
            "no_purge": True,
        }

        cls._log_event(
            "meta_training_input_validated",
            rows=total_rows,
            feature_schema_version=manifest_feature_schema_version,
            label_contract_version=META_TRAINING_LABEL_CONTRACT_VERSION,
            split_policy_version=META_TRAINING_SPLIT_POLICY_VERSION,
            sort_key=META_TRAINING_SORT_KEY,
            purge_policy=META_TRAINING_PURGE_POLICY,
        )
        cls._log_event(
            "meta_training_split_validated",
            split_policy_hash=split_policy_hash,
            total_rows=total_rows,
            train_row_count=manifest["train_row_count"],
            validation_row_count=manifest["validation_row_count"],
            test_row_count=manifest["test_row_count"],
            target_train_row_count=manifest["target_train_row_count"],
            target_validation_row_count=manifest["target_validation_row_count"],
            target_test_row_count=manifest["target_test_row_count"],
            train_cluster_count=manifest["train_cluster_count"],
            validation_cluster_count=manifest["validation_cluster_count"],
            test_cluster_count=manifest["test_cluster_count"],
            train_time_start=train_time_start,
            train_time_end=train_time_end,
            validation_time_start=validation_time_start,
            validation_time_end=validation_time_end,
            test_time_start=test_time_start,
            test_time_end=test_time_end,
            boundary_bucket_assignments=boundary_bucket_assignments,
            actual_minus_target_train=manifest["train_row_count"] - manifest["target_train_row_count"],
            actual_minus_target_validation=manifest["validation_row_count"] - manifest["target_validation_row_count"],
            actual_minus_target_test=manifest["test_row_count"] - manifest["target_test_row_count"],
        )
        return manifest

    async def _fetch_meta_materialization_source_rows(self) -> List[Dict[str, Any]]:
        rows = await self.ledger.execute(
            """
            WITH latest_idempotency AS (
                SELECT il.id,
                       il.idempotency_key,
                       il.order_id,
                       il.correlation_id,
                       il.status,
                       il.filled_quantity,
                       il.filled_price,
                       il.fees,
                       il.created_at,
                       il.updated_at
                FROM idempotency_log il
                WHERE il.id = (
                    SELECT il2.id
                    FROM idempotency_log il2
                    WHERE il2.order_id = il.order_id
                    ORDER BY COALESCE(NULLIF(il2.updated_at, ''), il2.created_at, '') DESC,
                             COALESCE(NULLIF(il2.created_at, ''), il2.updated_at, '') DESC,
                             il2.id DESC
                    LIMIT 1
                )
            )
            SELECT co.observation_id,
                   co.candidate_id,
                   co.cluster_id,
                   co.feature_snapshot_ts,
                   co.feature_schema_version,
                   co.cluster_policy_version,
                   co.training_eligibility,
                   co.market_id,
                   co.token_id,
                   co.market_question,
                   co.signal_side,
                   co.opportunity_side,
                   co.selected_side,
                   co.observation_source,
                   co.observation_mode,
                   co.raw_yes_prob,
                   co.yes_side_raw_probability,
                   co.calibrated_yes_prob,
                   co.selected_side_prob,
                   co.charlie_confidence,
                   co.charlie_implied_prob,
                   co.charlie_edge,
                   co.spread_bps,
                   co.time_to_expiry_seconds,
                   co.token_price,
                   co.normalized_yes_price,
                   co.timestamp,
                   co.observed_at,
                   co.resolution_time_hint,
                   co.guard_block_reason,
                   co.calibration_blocked,
                   co.trigger,
                   co.status,
                   co.actual_yes_outcome,
                   co.eventual_yes_market_outcome,
                   co.resolved_at,
                   co.resolution_time,
                   co.order_id,
                   co.trade_outcome,
                   ot.order_state,
                   ot.size AS order_requested_notional,
                   ot.price AS order_requested_price,
                   ot.opened_at AS order_opened_at,
                   ot.closed_at AS order_closed_at,
                   ot.pnl AS order_pnl,
                                     il.idempotency_key,
                                     il.status AS idempotency_status,
                                     il.filled_quantity AS filled_quantity,
                                     il.filled_price AS filled_price,
                                     il.fees AS fill_fees,
                                     il.created_at AS idempotency_created_at,
                                     il.updated_at AS idempotency_updated_at
            FROM calibration_observations co
            LEFT JOIN order_tracking ot
              ON ot.order_id = co.order_id
                        LEFT JOIN latest_idempotency il
              ON il.order_id = co.order_id
            ORDER BY co.feature_snapshot_ts ASC, co.candidate_id ASC, co.observation_id ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        return [dict(row) for row in rows]

    async def build_meta_materialization(
        self,
        *,
        min_positive_return_bps: Decimal | str = Decimal("0"),
        min_fill_ratio: Decimal | str = Decimal("1.0"),
    ) -> Dict[str, Any]:
        min_positive_return_bps_dec = Decimal(str(min_positive_return_bps))
        min_fill_ratio_dec = Decimal(str(min_fill_ratio))
        if min_fill_ratio_dec < Decimal("0") or min_fill_ratio_dec > Decimal("1"):
            raise ValueError("min_fill_ratio must be within [0, 1]")

        source_rows = await self._fetch_meta_materialization_source_rows()
        ordered_source_rows = sorted(source_rows, key=self._meta_sort_key)
        cluster_counts = Counter(
            str(row.get("cluster_id") or "")
            for row in ordered_source_rows
            if str(row.get("cluster_id") or "")
        )

        candidate_exhaust_rows: List[Dict[str, Any]] = []
        candidate_exhaust_source_rows: List[Dict[str, Any]] = []
        dropped_candidate_rows: List[Dict[str, Any]] = []
        seen_clusters: Dict[str, str] = {}

        for row in ordered_source_rows:
            candidate_id = str(row.get("candidate_id") or row.get("observation_id") or "")
            cluster_id = str(row.get("cluster_id") or "")
            feature_snapshot_ts = self._normalize_utc_text(row.get("feature_snapshot_ts") or row.get("observed_at") or row.get("timestamp"))
            if not candidate_id or not cluster_id or not feature_snapshot_ts:
                dropped_candidate_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "drop_reason": "invalid_candidate_contract",
                    }
                )
                continue
            if cluster_id in seen_clusters:
                dropped_candidate_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "drop_reason": "cluster_duplicate",
                        "kept_candidate_id": seen_clusters[cluster_id],
                    }
                )
                continue

            seen_clusters[cluster_id] = candidate_id
            candidate_exhaust_source_rows.append(row)
            candidate_exhaust_rows.append(
                {
                    "candidate_id": candidate_id,
                    "observation_id": str(row.get("observation_id") or ""),
                    "cluster_id": cluster_id,
                    "cluster_candidate_count": cluster_counts.get(cluster_id, 1),
                    "feature_snapshot_ts": feature_snapshot_ts,
                    "feature_schema_version": str(row.get("feature_schema_version") or ""),
                    "cluster_policy_version": str(row.get("cluster_policy_version") or ""),
                    "market_id": str(row.get("market_id") or ""),
                    "token_id": str(row.get("token_id") or ""),
                    "market_question": str(row.get("market_question") or ""),
                    "signal_side": str(row.get("signal_side") or ""),
                    "opportunity_side": str(row.get("opportunity_side") or ""),
                    "selected_side": str(row.get("selected_side") or ""),
                    "observation_source": str(row.get("observation_source") or ""),
                    "observation_mode": str(row.get("observation_mode") or ""),
                    "trigger": str(row.get("trigger") or ""),
                    "training_eligibility": str(row.get("training_eligibility") or ""),
                    "guard_block_reason": str(row.get("guard_block_reason") or ""),
                    "status": str(row.get("status") or ""),
                    "order_id": str(row.get("order_id") or ""),
                    "actual_yes_outcome": str(row.get("actual_yes_outcome") or ""),
                    "eventual_yes_market_outcome": str(row.get("eventual_yes_market_outcome") or ""),
                    "raw_yes_prob": str(row.get("raw_yes_prob") or ""),
                    "yes_side_raw_probability": str(row.get("yes_side_raw_probability") or row.get("raw_yes_prob") or ""),
                    "calibrated_yes_prob": str(row.get("calibrated_yes_prob") or ""),
                    "selected_side_prob": str(row.get("selected_side_prob") or ""),
                    "charlie_confidence": str(row.get("charlie_confidence") or ""),
                    "charlie_implied_prob": str(row.get("charlie_implied_prob") or ""),
                    "charlie_edge": str(row.get("charlie_edge") or ""),
                    "spread_bps": str(row.get("spread_bps") or ""),
                    "time_to_expiry_seconds": str(row.get("time_to_expiry_seconds") or ""),
                    "token_price": str(row.get("token_price") or ""),
                    "normalized_yes_price": str(row.get("normalized_yes_price") or ""),
                    "timestamp": self._normalize_utc_text(row.get("timestamp") or feature_snapshot_ts),
                    "observed_at": self._normalize_utc_text(row.get("observed_at") or feature_snapshot_ts),
                    "resolved_at": self._normalize_utc_text(row.get("resolved_at")),
                }
            )

        executed_profitability_rows: List[Dict[str, Any]] = []
        dropped_executed_rows: List[Dict[str, Any]] = []

        for row in candidate_exhaust_source_rows:
            candidate_id = str(row.get("candidate_id") or row.get("observation_id") or "")
            cluster_id = str(row.get("cluster_id") or "")
            training_eligibility = str(row.get("training_eligibility") or "")
            order_id = str(row.get("order_id") or "")
            status = str(row.get("status") or "")
            order_state = str(row.get("order_state") or "").upper()

            if training_eligibility != "pending_resolution":
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "drop_reason": f"training_eligibility:{training_eligibility or 'missing'}",
                    }
                )
                continue
            if not order_id:
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "drop_reason": "missing_order_id",
                    }
                )
                continue
            if status.lower() != "resolved" or str(row.get("actual_yes_outcome") or "") == "":
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "unresolved_observation",
                    }
                )
                continue
            if not order_state:
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "missing_order_tracking",
                    }
                )
                continue
            if order_state in {"CANCELLED", "ERROR", "EXPIRED", "SUPERSEDED", "REJECTED"}:
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": f"order_state:{order_state.lower()}",
                    }
                )
                continue

            requested_notional = self._to_decimal(row.get("order_requested_notional"), default=Decimal("0"))
            requested_price = self._to_decimal(row.get("order_requested_price"), default=Decimal("0"))
            if requested_notional is None or requested_notional <= Decimal("0") or requested_price is None or requested_price <= Decimal("0"):
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "invalid_requested_order_shape",
                    }
                )
                continue

            requested_quantity = requested_notional / requested_price
            filled_quantity = self._to_decimal(row.get("filled_quantity"))
            filled_price = self._to_decimal(row.get("filled_price"), default=requested_price)
            if filled_quantity is None and order_state in {"FILLED", "SETTLED"}:
                filled_quantity = requested_quantity
            if filled_quantity is None:
                filled_quantity = Decimal("0")
            fill_ratio = (filled_quantity / requested_quantity) if requested_quantity > Decimal("0") else Decimal("0")

            if order_state == "PARTIALLY_FILLED" or fill_ratio < min_fill_ratio_dec:
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "partial_fill_excluded",
                        "fill_ratio": str(round(fill_ratio, 6)),
                    }
                )
                continue
            if order_state != "SETTLED":
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": f"order_state:{order_state.lower()}",
                    }
                )
                continue

            settled_pnl = self._to_decimal(row.get("order_pnl"))
            if settled_pnl is None:
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "missing_settled_pnl",
                    }
                )
                continue

            filled_notional = (filled_quantity * filled_price) if filled_price is not None and filled_quantity > Decimal("0") else requested_notional
            if filled_notional <= Decimal("0"):
                dropped_executed_rows.append(
                    {
                        "candidate_id": candidate_id,
                        "observation_id": str(row.get("observation_id") or ""),
                        "cluster_id": cluster_id,
                        "order_id": order_id,
                        "drop_reason": "non_positive_filled_notional",
                    }
                )
                continue

            realized_return_bps = (settled_pnl / filled_notional) * Decimal("10000")
            profitability_label = int(realized_return_bps >= min_positive_return_bps_dec)
            executed_profitability_rows.append(
                {
                    "candidate_id": candidate_id,
                    "observation_id": str(row.get("observation_id") or ""),
                    "cluster_id": cluster_id,
                    "feature_snapshot_ts": self._normalize_utc_text(row.get("feature_snapshot_ts") or row.get("observed_at") or row.get("timestamp")),
                    "feature_schema_version": str(row.get("feature_schema_version") or ""),
                    "cluster_policy_version": str(row.get("cluster_policy_version") or ""),
                    "market_id": str(row.get("market_id") or ""),
                    "token_id": str(row.get("token_id") or ""),
                    "market_question": str(row.get("market_question") or ""),
                    "selected_side": str(row.get("selected_side") or ""),
                    "order_id": order_id,
                    "order_state": order_state,
                    "order_opened_at": self._normalize_utc_text(row.get("order_opened_at")),
                    "order_closed_at": self._normalize_utc_text(row.get("order_closed_at")),
                    "requested_notional": str(round(requested_notional, 8)),
                    "requested_quantity": str(round(requested_quantity, 8)),
                    "filled_quantity": str(round(filled_quantity, 8)),
                    "filled_price": str(round(filled_price or requested_price, 8)),
                    "fill_ratio": str(round(fill_ratio, 6)),
                    "min_fill_ratio": str(min_fill_ratio_dec),
                    "min_positive_return_bps": str(min_positive_return_bps_dec),
                    "settled_pnl": str(round(settled_pnl, 8)),
                    "realized_return_bps": str(round(realized_return_bps, 6)),
                    "profitability_label": profitability_label,
                    "actual_yes_outcome": str(row.get("actual_yes_outcome") or ""),
                    "eventual_yes_market_outcome": str(row.get("eventual_yes_market_outcome") or ""),
                    "training_eligibility": training_eligibility,
                    "raw_yes_prob": str(row.get("raw_yes_prob") or ""),
                    "yes_side_raw_probability": str(row.get("yes_side_raw_probability") or row.get("raw_yes_prob") or ""),
                    "calibrated_yes_prob": str(row.get("calibrated_yes_prob") or ""),
                    "selected_side_prob": str(row.get("selected_side_prob") or ""),
                    "charlie_confidence": str(row.get("charlie_confidence") or ""),
                    "charlie_implied_prob": str(row.get("charlie_implied_prob") or ""),
                    "charlie_edge": str(row.get("charlie_edge") or ""),
                    "spread_bps": str(row.get("spread_bps") or ""),
                    "time_to_expiry_seconds": str(row.get("time_to_expiry_seconds") or ""),
                    "token_price": str(row.get("token_price") or ""),
                    "normalized_yes_price": str(row.get("normalized_yes_price") or ""),
                }
            )

        self._log_event(
            "meta_dataset_materialized",
            candidate_exhaust_rows=len(candidate_exhaust_rows),
            executed_profitability_rows=len(executed_profitability_rows),
            dropped_candidate_rows=len(dropped_candidate_rows),
            dropped_executed_rows=len(dropped_executed_rows),
            min_positive_return_bps=str(min_positive_return_bps_dec),
            min_fill_ratio=str(min_fill_ratio_dec),
        )
        return {
            "label_contract": {
                "min_positive_return_bps": str(min_positive_return_bps_dec),
                "min_fill_ratio": str(min_fill_ratio_dec),
                "partial_fill_policy": "exclude_when_fill_ratio_below_threshold",
            },
            "candidate_exhaust": candidate_exhaust_rows,
            "executed_profitability": executed_profitability_rows,
            "dropped_candidate_rows": dropped_candidate_rows,
            "dropped_executed_rows": dropped_executed_rows,
        }

    async def materialize_meta_datasets(
        self,
        *,
        candidate_exhaust_path: str,
        executed_profitability_path: str,
        split_manifest_path: str,
        min_positive_return_bps: Decimal | str = Decimal("0"),
        min_fill_ratio: Decimal | str = Decimal("1.0"),
    ) -> Dict[str, Any]:
        materialized = await self.build_meta_materialization(
            min_positive_return_bps=min_positive_return_bps,
            min_fill_ratio=min_fill_ratio,
        )
        candidate_path = Path(candidate_exhaust_path)
        executed_path = Path(executed_profitability_path)
        split_manifest_artifact_path = Path(split_manifest_path)
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        executed_path.parent.mkdir(parents=True, exist_ok=True)
        split_manifest_artifact_path.parent.mkdir(parents=True, exist_ok=True)

        with open(candidate_path, "w", newline="", encoding="utf-8") as candidate_file:
            writer = csv.DictWriter(candidate_file, fieldnames=META_CANDIDATE_EXHAUST_FIELDNAMES)
            writer.writeheader()
            for row in materialized["candidate_exhaust"]:
                writer.writerow({field: row.get(field, "") for field in META_CANDIDATE_EXHAUST_FIELDNAMES})

        with open(executed_path, "w", newline="", encoding="utf-8") as executed_file:
            writer = csv.DictWriter(executed_file, fieldnames=META_EXECUTED_PROFITABILITY_FIELDNAMES)
            writer.writeheader()
            for row in materialized["executed_profitability"]:
                writer.writerow({field: row.get(field, "") for field in META_EXECUTED_PROFITABILITY_FIELDNAMES})

        split_manifest = self.build_training_split_manifest(
            materialized["executed_profitability"],
            feature_schema_version=self.feature_schema_version,
        )
        with open(split_manifest_artifact_path, "w", encoding="utf-8") as split_manifest_file:
            json.dump(split_manifest, split_manifest_file, indent=2, sort_keys=True)
            split_manifest_file.write("\n")

        return {
            **materialized,
            "candidate_exhaust_path": str(candidate_path),
            "executed_profitability_path": str(executed_path),
            "split_manifest": split_manifest,
            "split_manifest_path": str(split_manifest_artifact_path),
        }

    async def record_observation(
        self,
        *,
        market_id: str,
        token_id: str,
        opportunity: Dict[str, Any],
        charlie_rec,
        token_price: Decimal,
        normalized_yes_price: Decimal,
        trigger: str,
        observation_mode: str,
        calibration_blocked: bool,
        guard_block_reason: str = "",
    ) -> str:
        observation_id = uuid.uuid4().hex
        observed_at = self._utc_now_iso()
        selected_side = str(charlie_rec.side)
        cluster_id = self.compute_cluster_id(
            market_id=market_id,
            selected_side=selected_side,
            trigger=trigger,
            feature_snapshot_ts=observed_at,
            token_price=token_price,
        )
        time_to_expiry_seconds = self._time_to_expiry_seconds(opportunity, observed_at)
        row = {
            "observation_id": observation_id,
            # V1 alias by design: `candidate_id` is the persisted row identity.
            # Split this from `observation_id` only when one semantic candidate
            # can spawn multiple child observation/execution/enrichment records.
            "candidate_id": observation_id,
            "cluster_id": cluster_id,
            "feature_snapshot_ts": observed_at,
            "feature_schema_version": self.feature_schema_version,
            "cluster_policy_version": self.cluster_policy_version,
            "training_eligibility": "pending_execution",
            "market_id": market_id,
            "token_id": token_id,
            "market_question": str(opportunity.get("question") or ""),
            "signal_side": str(opportunity.get("side") or "").upper(),
            "opportunity_side": str(opportunity.get("side") or "").upper(),
            "selected_side": selected_side,
            "observation_source": "charlie_scored_opportunity",
            "observation_mode": observation_mode,
            "raw_yes_prob": str(round(float(charlie_rec.p_win_raw), 6)),
            "yes_side_raw_probability": str(round(float(charlie_rec.p_win_raw), 6)),
            "calibrated_yes_prob": str(round(float(charlie_rec.p_win_calibrated), 6)),
            "selected_side_prob": str(round(float(charlie_rec.p_win), 6)),
            "charlie_confidence": str(round(float(charlie_rec.confidence), 6)),
            "charlie_implied_prob": str(round(float(charlie_rec.implied_prob), 6)),
            "charlie_edge": str(round(float(charlie_rec.edge), 6)),
            "spread_bps": str(opportunity.get("spread_bps") or ""),
            "time_to_expiry_seconds": time_to_expiry_seconds if time_to_expiry_seconds is not None else "",
            "token_price": str(token_price),
            "normalized_yes_price": str(normalized_yes_price),
            "timestamp": observed_at,
            "observed_at": observed_at,
            "resolution_time_hint": str(
                opportunity.get("end_time")
                or opportunity.get("endDate")
                or opportunity.get("endDateIso")
                or ""
            ),
            "guard_block_reason": guard_block_reason,
            "calibration_blocked": "true" if calibration_blocked else "false",
            "trigger": trigger,
            "status": "pending",
            "actual_yes_outcome": "",
            "eventual_yes_market_outcome": "",
            "resolved_at": "",
            "resolution_time": "",
            "order_id": "",
            "trade_outcome": "",
            "created_at": observed_at,
            "updated_at": observed_at,
        }
        await self.ledger.execute(
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
                row["observation_id"],
                row["candidate_id"],
                row["cluster_id"],
                row["feature_snapshot_ts"],
                row["feature_schema_version"],
                row["cluster_policy_version"],
                row["training_eligibility"],
                row["market_id"],
                row["token_id"],
                row["market_question"],
                row["signal_side"],
                row["opportunity_side"],
                row["selected_side"],
                row["observation_source"],
                row["observation_mode"],
                row["raw_yes_prob"],
                row["yes_side_raw_probability"],
                row["calibrated_yes_prob"],
                row["selected_side_prob"],
                row["charlie_confidence"],
                row["charlie_implied_prob"],
                row["charlie_edge"],
                row["spread_bps"],
                row["time_to_expiry_seconds"],
                row["token_price"],
                row["normalized_yes_price"],
                row["timestamp"],
                row["observed_at"],
                row["resolution_time_hint"],
                row["guard_block_reason"],
                1 if calibration_blocked else 0,
                row["trigger"],
                row["status"],
                row["actual_yes_outcome"],
                row["eventual_yes_market_outcome"],
                row["resolved_at"],
                row["resolution_time"],
                row["order_id"],
                row["trade_outcome"],
                row["created_at"],
                row["updated_at"],
            ),
            commit=True,
        )
        self._log_event(
            "meta_candidate_row_written",
            candidate_id=row["candidate_id"],
            cluster_id=row["cluster_id"],
            feature_snapshot_ts=row["feature_snapshot_ts"],
            market_id=row["market_id"],
            trigger=row["trigger"],
        )
        self._log_event(
            "meta_candidate_schema_version",
            candidate_id=row["candidate_id"],
            feature_schema_version=row["feature_schema_version"],
            cluster_policy_version=row["cluster_policy_version"],
        )
        self._log_event(
            "meta_candidate_training_eligibility",
            candidate_id=row["candidate_id"],
            training_eligibility=row["training_eligibility"],
        )
        self._log_event(
            "meta_candidate_cluster_id",
            candidate_id=row["candidate_id"],
            cluster_id=row["cluster_id"],
        )
        return observation_id

    async def update_observation(self, observation_id: Optional[str], **updates: Any) -> None:
        if not observation_id:
            return
        valid_updates = {
            key: value
            for key, value in updates.items()
            if key in {
                "guard_block_reason",
                "status",
                "training_eligibility",
                "actual_yes_outcome",
                "eventual_yes_market_outcome",
                "resolved_at",
                "resolution_time",
                "order_id",
                "trade_outcome",
            }
            and value is not None
        }
        if not valid_updates:
            return

        assignments = ", ".join(f"{key} = ?" for key in valid_updates)
        params = [
            self._normalize_utc_text(value) if key in {"resolved_at", "resolution_time"} else str(value)
            for key, value in valid_updates.items()
        ]
        params.append(self._utc_now_iso())
        params.append(observation_id)
        await self.ledger.execute(
            f"UPDATE calibration_observations SET {assignments}, updated_at = ? WHERE observation_id = ?",
            tuple(params),
            commit=True,
        )

    @staticmethod
    def extract_actual_yes_outcome_from_market(market: Dict[str, Any]) -> Optional[int]:
        if not isinstance(market, dict) or not market:
            return None

        winning_side = str(market.get("winning_side") or market.get("outcome") or "").upper()
        if winning_side in {"YES", "UP", "TRUE"}:
            return 1
        if winning_side in {"NO", "DOWN", "FALSE"}:
            return 0

        outcome_prices_raw = market.get("outcomePrices")
        if outcome_prices_raw is None:
            return None
        try:
            outcome_prices = (
                json.loads(outcome_prices_raw)
                if isinstance(outcome_prices_raw, str)
                else outcome_prices_raw
            )
            if not isinstance(outcome_prices, list) or len(outcome_prices) < 2:
                return None
            yes_price = Decimal(str(outcome_prices[0]))
            no_price = Decimal(str(outcome_prices[1]))
            if yes_price >= Decimal("0.999"):
                return 1
            if no_price >= Decimal("0.999"):
                return 0
        except Exception:
            return None
        return None

    async def resolve_pending_observations(self, api_client, safe_await) -> int:
        if api_client is None:
            return 0
        rows = await self.ledger.execute(
            """
            SELECT observation_id, market_id
            FROM calibration_observations
            WHERE status = 'pending'
            ORDER BY observed_at ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        if not rows:
            return 0

        resolved_count = 0
        market_cache: Dict[str, Any] = {}
        for row in rows:
            market_id = str(row.get("market_id") or "")
            observation_id = str(row.get("observation_id") or "")
            if not market_id or not observation_id:
                continue
            if market_id not in market_cache:
                market_cache[market_id] = await safe_await(
                    f"api_client.get_market.calibration_observation.{market_id}",
                    api_client.get_market(market_id),
                    timeout_seconds=8.0,
                    default=None,
                ) if hasattr(api_client, "get_market") else None
            market = market_cache.get(market_id) or {}
            actual_yes_outcome = self.extract_actual_yes_outcome_from_market(market)
            if actual_yes_outcome is None:
                continue
            await self.ledger.execute(
                """
                UPDATE calibration_observations
                SET status = ?,
                    actual_yes_outcome = ?,
                    eventual_yes_market_outcome = ?,
                    resolved_at = ?,
                    resolution_time = ?,
                    updated_at = ?
                WHERE observation_id = ?
                """,
                (
                    "resolved",
                    str(actual_yes_outcome),
                    str(actual_yes_outcome),
                    self._utc_now_iso(),
                    self._normalize_utc_text(
                        market.get("endDate") or market.get("endDateIso") or market.get("resolutionTime") or ""
                    ),
                    self._utc_now_iso(),
                    observation_id,
                ),
                commit=True,
            )
            resolved_count += 1

        return resolved_count

    async def record_settled_trade_fallback(
        self,
        *,
        market_id: str,
        order_id: str,
        signal_side: str,
        selected_side: str,
        raw_yes_prob: str,
        calibrated_yes_prob: str,
        selected_side_prob: str,
        token_price: str,
        normalized_yes_price: str,
        timestamp: str,
        resolution_time: str,
        actual_yes_outcome: int,
        trade_outcome: int,
    ) -> str:
        observation_id = f"settled-fallback-{order_id}"
        normalized_timestamp = self._normalize_utc_text(timestamp, default_now=True)
        normalized_resolution_time = self._normalize_utc_text(resolution_time)
        written_at = self._utc_now_iso()
        await self.ledger.execute(
            """
            INSERT OR REPLACE INTO calibration_observations (
                observation_id, market_id, token_id, market_question,
                signal_side, opportunity_side, selected_side,
                observation_source, observation_mode,
                raw_yes_prob, yes_side_raw_probability, calibrated_yes_prob, selected_side_prob,
                token_price, normalized_yes_price,
                timestamp, observed_at, resolution_time_hint,
                guard_block_reason, calibration_blocked, trigger,
                status, actual_yes_outcome, eventual_yes_market_outcome,
                resolved_at, resolution_time, order_id, trade_outcome,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                market_id,
                "",
                "",
                signal_side,
                signal_side,
                selected_side,
                "settled_trade_fallback",
                "trade_enabled",
                str(raw_yes_prob),
                str(raw_yes_prob),
                str(calibrated_yes_prob),
                str(selected_side_prob) if selected_side_prob is not None else "",
                str(token_price) if token_price is not None else "",
                str(normalized_yes_price) if normalized_yes_price is not None else "",
                normalized_timestamp,
                normalized_timestamp,
                "",
                "",
                0,
                "settlement_fallback",
                "resolved",
                str(actual_yes_outcome),
                str(actual_yes_outcome),
                written_at,
                normalized_resolution_time,
                order_id,
                str(trade_outcome),
                written_at,
                written_at,
            ),
            commit=True,
        )
        return observation_id

    async def export_csv_artifacts(
        self,
        *,
        observations: bool = True,
        dataset: bool = True,
    ) -> None:
        if observations:
            await self.export_observations_csv()
        if dataset:
            await self.export_dataset_csv()

    async def export_observations_csv(self) -> None:
        rows = await self.ledger.execute(
            """
            SELECT observation_id, market_id, token_id, market_question,
                   signal_side, opportunity_side, selected_side,
                   observation_source, observation_mode,
                   raw_yes_prob, yes_side_raw_probability, calibrated_yes_prob, selected_side_prob,
                   token_price, normalized_yes_price,
                   timestamp, observed_at, resolution_time_hint,
                   guard_block_reason,
                   CASE WHEN calibration_blocked = 1 THEN 'true' ELSE 'false' END AS calibration_blocked,
                   trigger, status, actual_yes_outcome, eventual_yes_market_outcome, resolved_at
            FROM calibration_observations
            ORDER BY observed_at ASC, observation_id ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        self.observation_export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.observation_export_path, "w", newline="", encoding="utf-8") as observation_file:
            writer = csv.DictWriter(observation_file, fieldnames=CALIBRATION_OBSERVATION_FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CALIBRATION_OBSERVATION_FIELDNAMES})

    async def export_dataset_csv(self) -> None:
        rows = await self.ledger.execute(
            """
            SELECT observation_id, market_id, signal_side, selected_side,
                   observation_source, observation_mode,
                   raw_yes_prob, yes_side_raw_probability, calibrated_yes_prob, selected_side_prob,
                   actual_yes_outcome, eventual_yes_market_outcome, trade_outcome,
                   token_price, normalized_yes_price,
                   timestamp, observed_at, resolution_time, guard_block_reason, order_id
            FROM calibration_observations
            WHERE status = 'resolved' AND actual_yes_outcome IS NOT NULL AND actual_yes_outcome != ''
            ORDER BY COALESCE(resolved_at, observed_at) ASC, observation_id ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        self.dataset_export_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dataset_export_path, "w", newline="", encoding="utf-8") as dataset_file:
            writer = csv.DictWriter(dataset_file, fieldnames=CALIBRATION_DATASET_FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "schema_version": 2,
                        "feature_space": "yes_side_raw_probability",
                        "label_space": "yes_market_outcome",
                        "market_id": row.get("market_id"),
                        "observation_id": row.get("observation_id"),
                        "order_id": row.get("order_id") or "",
                        "signal_side": row.get("signal_side"),
                        "trade_side": row.get("selected_side"),
                        "selected_side": row.get("selected_side"),
                        "observation_source": row.get("observation_source"),
                        "observation_mode": row.get("observation_mode"),
                        "raw_yes_prob": row.get("raw_yes_prob"),
                        "yes_side_raw_probability": row.get("yes_side_raw_probability") or row.get("raw_yes_prob"),
                        "calibrated_yes_prob": row.get("calibrated_yes_prob"),
                        "selected_side_prob": row.get("selected_side_prob"),
                        "actual_yes_outcome": row.get("actual_yes_outcome"),
                        "eventual_yes_market_outcome": row.get("eventual_yes_market_outcome"),
                        "trade_outcome": row.get("trade_outcome") or "",
                        "token_price": row.get("token_price"),
                        "normalized_yes_price": row.get("normalized_yes_price"),
                        "timestamp": row.get("timestamp") or row.get("observed_at"),
                        "entry_time": row.get("observed_at"),
                        "resolution_time": row.get("resolution_time"),
                        "guard_block_reason": row.get("guard_block_reason") or "",
                    }
                )

    async def build_readiness_report(self) -> Dict[str, Any]:
        total_observations = int(
            await self.ledger.execute_scalar(
                "SELECT COUNT(*) FROM calibration_observations"
            ) or 0
        )
        total_resolved = int(
            await self.ledger.execute_scalar(
                "SELECT COUNT(*) FROM calibration_observations WHERE status = 'resolved'"
            ) or 0
        )
        eligible_rows = int(
            await self.ledger.execute_scalar(
                """
                SELECT COUNT(*)
                FROM calibration_observations
                WHERE status = 'resolved'
                  AND actual_yes_outcome IS NOT NULL
                  AND actual_yes_outcome != ''
                  AND yes_side_raw_probability IS NOT NULL
                  AND yes_side_raw_probability != ''
                """
            ) or 0
        )
        label_rows = await self.ledger.execute(
            """
            SELECT actual_yes_outcome AS label, COUNT(*) AS total
            FROM calibration_observations
            WHERE status = 'resolved' AND actual_yes_outcome IS NOT NULL AND actual_yes_outcome != ''
            GROUP BY actual_yes_outcome
            ORDER BY actual_yes_outcome ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        selected_side_rows = await self.ledger.execute(
            """
            SELECT selected_side AS side, COUNT(*) AS total
            FROM calibration_observations
            WHERE selected_side IS NOT NULL AND selected_side != ''
            GROUP BY selected_side
            ORDER BY selected_side ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        signal_side_rows = await self.ledger.execute(
            """
            SELECT signal_side AS side, COUNT(*) AS total
            FROM calibration_observations
            WHERE signal_side IS NOT NULL AND signal_side != ''
            GROUP BY signal_side
            ORDER BY signal_side ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        market_rows = await self.ledger.execute(
            """
            SELECT market_id
            FROM calibration_observations
            WHERE market_id IS NOT NULL AND market_id != ''
            GROUP BY market_id
            ORDER BY market_id ASC
            """,
            fetch_all=True,
            as_dict=True,
        ) or []
        return {
            "total_schema_v2_observations": total_observations,
            "total_resolved": total_resolved,
            "rows_eligible_for_calibration": eligible_rows,
            "label_distribution": {str(row.get('label')): int(row.get('total') or 0) for row in label_rows},
            "selected_side_distribution": {str(row.get('side')): int(row.get('total') or 0) for row in selected_side_rows},
            "signal_side_distribution": {str(row.get('side')): int(row.get('total') or 0) for row in signal_side_rows},
            "markets_covered": [str(row.get('market_id')) for row in market_rows],
        }
