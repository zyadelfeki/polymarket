from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple


class RuntimeGuardEvaluator:
    """Evaluates runtime safety guards without owning trading-system state."""

    def __init__(
        self,
        *,
        lifecycle_guard_config: Dict[str, Any] | None = None,
        calibration_guard_config: Dict[str, Any] | None = None,
    ) -> None:
        self.lifecycle_guard_config = lifecycle_guard_config or {}
        self.calibration_guard_config = calibration_guard_config or {}

    def evaluate_calibration_guard(self) -> Dict[str, Any]:
        """Run startup calibration safety checks."""
        from models.calibration import calibrate_p_win as smoke_calibration

        calibration_cfg = self.calibration_guard_config or {}
        smoke_points = [
            float(value)
            for value in calibration_cfg.get("smoke_test_points", [0.30, 0.50, 0.79])
        ]
        smoke_results: List[Dict[str, float]] = []
        for raw_value in smoke_points:
            calibrated = float(smoke_calibration(raw_value))
            smoke_results.append(
                {
                    "raw": raw_value,
                    "calibrated": calibrated,
                    "delta": abs(calibrated - raw_value),
                }
            )

        calibrated_values = [item["calibrated"] for item in smoke_results]
        monotonic = calibrated_values == sorted(calibrated_values)
        coef = None
        scaler_exists = Path("models/platt_scaler.pkl").exists()
        if scaler_exists:
            import joblib

            scaler = joblib.load("models/platt_scaler.pkl")
            coef = float(scaler.coef_[0][0])

        blocked = False
        reasons: List[str] = []
        if coef is not None and coef <= float(calibration_cfg.get("min_positive_coef", 0.0)):
            blocked = bool(calibration_cfg.get("fail_closed", True))
            reasons.append(f"non_positive_coef={coef:.6f}")
        if calibration_cfg.get("require_monotonic_smoke_test", True) and not monotonic:
            blocked = bool(calibration_cfg.get("fail_closed", True))
            reasons.append("non_monotonic_smoke_test")

        return {
            "blocked": blocked,
            "reason": ",".join(reasons) if reasons else None,
            "coef": coef,
            "monotonic": monotonic,
            "smoke_results": smoke_results,
            "scaler_exists": scaler_exists,
        }

    @staticmethod
    def calibration_block_log_context(
        calibration_guard_status: Dict[str, Any],
        *,
        observe_only: bool,
    ) -> Dict[str, Any]:
        return {
            "reason": calibration_guard_status.get("reason"),
            "coef": calibration_guard_status.get("coef"),
            "monotonic": calibration_guard_status.get("monotonic"),
            "observe_only": observe_only,
        }

    @staticmethod
    def lifecycle_block_event_name(reason: str) -> str:
        return "blocked_by_side_flip_rule" if reason == "side_flip_rule" else "blocked_by_lifecycle_guard"

    def check_lifecycle_guard(
        self,
        lifecycle_state: Dict[str, Dict[str, Any]],
        *,
        market_id: str,
        side: str,
        token_price: Decimal,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Return `(blocked, reason, context)` for lifecycle guard decisions."""
        lifecycle_cfg = self.lifecycle_guard_config or {}
        if not lifecycle_cfg.get("enabled", True):
            return False, "", {}

        state = lifecycle_state.get(market_id)
        if not state:
            return False, "", {}

        existing_side = str(state.get("side") or "")
        entry_count = int(state.get("entry_count") or 0)
        max_entries = max(1, int(lifecycle_cfg.get("max_active_entries_per_market", 1)))
        allow_add_on = bool(lifecycle_cfg.get("allow_add_on", False))
        best_token_price = Decimal(str(state.get("best_token_price") or token_price))

        if existing_side and existing_side != side:
            return True, "side_flip_rule", {
                "existing_side": existing_side,
                "requested_side": side,
                "entry_count": entry_count,
            }

        if entry_count >= max_entries:
            return True, "lifecycle_guard", {
                "entry_count": entry_count,
                "max_entries": max_entries,
                "allow_add_on": allow_add_on,
            }

        if entry_count >= 1:
            if not allow_add_on:
                return True, "lifecycle_guard", {
                    "entry_count": entry_count,
                    "max_entries": max_entries,
                    "allow_add_on": allow_add_on,
                }

            min_improvement_abs = Decimal(
                str(lifecycle_cfg.get("min_price_improvement_abs", "0.05"))
            )
            actual_improvement = best_token_price - token_price
            if actual_improvement < min_improvement_abs:
                return True, "lifecycle_guard", {
                    "entry_count": entry_count,
                    "best_token_price": str(best_token_price),
                    "requested_token_price": str(token_price),
                    "actual_improvement": str(actual_improvement),
                    "required_improvement": str(min_improvement_abs),
                    "allow_add_on": allow_add_on,
                }

        return False, "", {
            "entry_count": entry_count,
            "max_entries": max_entries,
            "allow_add_on": allow_add_on,
        }
