"""Session-level trading invariants checker.

A vibe-coded project's main silent killer: events that "look fine" individually
but violate a system-level contract that only shows up as a PnL bleed later.

This module formalizes the invariants that MUST hold in any valid session and
provides a single ``check_session_invariants()`` call that returns structured
violations — not raw log counts.

Invariants checked
------------------
1. ORDER_SUBMISSION_RATE
   If charlie_gate_approved > 0 then order_submitted must also be > 0,
   UNLESS polymarket_api_unavailable is also non-zero (known outage).
   Violation = approved bets that never reached the exchange.

2. REJECTION_DOMINANCE
   If charlie_gate_rejected > (charlie_gate_approved * REJECTION_RATIO_WARN),
   warn that the gate is rejecting overwhelmingly — may indicate model/config drift.

3. DEDUP_SANITY
   charlie_gate_dedup_cache_hit should be < charlie_gate_approved * DEDUP_RATIO_MAX.
   Extremely high dedup hit rate suggests the main loop is spinning too fast.

4. FEED_HEALTH_AT_APPROVAL
   If binance_feed_unhealthy events exist in the same window as
   charlie_gate_approved, flag it — orders may have been submitted on stale data.

5. EXCEPTION_RATE
   If charlie_gate_exception events > EXCEPTION_THRESHOLD_ABS, flag it.
   Any gate exception is a contract violation and warrants investigation.

Usage::

    from infra.invariants import check_session_invariants, InvariantSeverity

    counts = {
        "charlie_gate_approved": 3,
        "charlie_gate_rejected": 41,
        "charlie_gate_exception": 0,
        "charlie_gate_dedup_cache_hit": 5,
        "order_submitted": 3,
        "polymarket_api_unavailable": 0,
        "binance_feed_unhealthy": 0,
    }
    violations = check_session_invariants(counts)
    for v in violations:
        print(f"[{v.severity.name}] {v.name}: {v.message}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, List


# ---------------------------------------------------------------------------
# Severity & Violation dataclass
# ---------------------------------------------------------------------------

class InvariantSeverity(Enum):
    OK      = auto()
    WARNING = auto()
    ERROR   = auto()   # Stop / investigate immediately


@dataclass
class InvariantViolation:
    name:     str
    severity: InvariantSeverity
    message:  str
    counts:   Dict = field(default_factory=dict)   # relevant counts for context


# ---------------------------------------------------------------------------
# Thresholds (can be overridden via kwargs on check_session_invariants)
# ---------------------------------------------------------------------------

_DEFAULT_REJECTION_RATIO_WARN   = 15.0   # reject:approve ratio above this → warn
_DEFAULT_DEDUP_RATIO_MAX        = 5.0    # dedup_hits:approvals above this → warn
_DEFAULT_EXCEPTION_THRESHOLD    = 0      # any exception → error


# ---------------------------------------------------------------------------
# Individual invariant checks
# ---------------------------------------------------------------------------

def _check_order_submission_rate(
    counts: Dict,
) -> List[InvariantViolation]:
    approved   = int(counts.get("charlie_gate_approved", 0))
    submitted  = int(counts.get("order_submitted", 0))
    api_outage = int(counts.get("polymarket_api_unavailable", 0))

    if approved > 0 and submitted == 0 and api_outage == 0:
        return [
            InvariantViolation(
                name="ORDER_SUBMISSION_RATE",
                severity=InvariantSeverity.ERROR,
                message=(
                    f"charlie_gate_approved={approved} but order_submitted=0 "
                    "and no API outage recorded. "
                    "Approved bets are NOT reaching the exchange."
                ),
                counts={"approved": approved, "submitted": submitted, "api_outage": api_outage},
            )
        ]
    if approved > 0 and submitted == 0 and api_outage > 0:
        return [
            InvariantViolation(
                name="ORDER_SUBMISSION_RATE",
                severity=InvariantSeverity.WARNING,
                message=(
                    f"charlie_gate_approved={approved} but order_submitted=0. "
                    f"Likely caused by polymarket_api_unavailable={api_outage}. "
                    "Investigate if outage is resolved."
                ),
                counts={"approved": approved, "submitted": submitted, "api_outage": api_outage},
            )
        ]
    return []


def _check_rejection_dominance(
    counts: Dict,
    rejection_ratio_warn: float,
) -> List[InvariantViolation]:
    approved  = int(counts.get("charlie_gate_approved", 0))
    rejected  = int(counts.get("charlie_gate_rejected", 0))

    if approved == 0 and rejected > 0:
        return [
            InvariantViolation(
                name="REJECTION_DOMINANCE",
                severity=InvariantSeverity.WARNING,
                message=(
                    f"charlie_gate_rejected={rejected} but charlie_gate_approved=0. "
                    "Check gate thresholds, charlie signal quality, and feed health."
                ),
                counts={"approved": approved, "rejected": rejected},
            )
        ]

    if approved > 0 and rejected > approved * rejection_ratio_warn:
        ratio = rejected / approved
        return [
            InvariantViolation(
                name="REJECTION_DOMINANCE",
                severity=InvariantSeverity.WARNING,
                message=(
                    f"Rejection ratio={ratio:.1f}x (rejected={rejected}, approved={approved}). "
                    f"Exceeds warning threshold of {rejection_ratio_warn:.0f}x. "
                    "May indicate min_edge or min_confidence thresholds are too aggressive, "
                    "or model/market drift."
                ),
                counts={"approved": approved, "rejected": rejected, "ratio": ratio},
            )
        ]
    return []


def _check_dedup_sanity(
    counts: Dict,
    dedup_ratio_max: float,
) -> List[InvariantViolation]:
    approved    = int(counts.get("charlie_gate_approved", 0))
    dedup_hits  = int(counts.get("charlie_gate_dedup_cache_hit", 0))

    if approved > 0 and dedup_hits > approved * dedup_ratio_max:
        ratio = dedup_hits / approved
        return [
            InvariantViolation(
                name="DEDUP_SANITY",
                severity=InvariantSeverity.WARNING,
                message=(
                    f"dedup_cache_hit={dedup_hits} is {ratio:.1f}x approvals={approved}. "
                    f"Exceeds threshold {dedup_ratio_max:.0f}x. "
                    "Main loop may be polling too aggressively (< 60s per cycle)."
                ),
                counts={"approved": approved, "dedup_hits": dedup_hits, "ratio": ratio},
            )
        ]
    return []


def _check_feed_health_at_approval(
    counts: Dict,
) -> List[InvariantViolation]:
    approved          = int(counts.get("charlie_gate_approved", 0))
    feed_unhealthy    = int(counts.get("binance_feed_unhealthy", 0))

    if approved > 0 and feed_unhealthy > 0:
        return [
            InvariantViolation(
                name="FEED_HEALTH_AT_APPROVAL",
                severity=InvariantSeverity.ERROR,
                message=(
                    f"charlie_gate_approved={approved} occurred during same session as "
                    f"binance_feed_unhealthy={feed_unhealthy}. "
                    "Orders may have been submitted on stale BTC price/feature data."
                ),
                counts={"approved": approved, "feed_unhealthy": feed_unhealthy},
            )
        ]
    return []


def _check_exception_rate(
    counts: Dict,
    exception_threshold: int,
) -> List[InvariantViolation]:
    exceptions = int(counts.get("charlie_gate_exception", 0))
    if exceptions > exception_threshold:
        return [
            InvariantViolation(
                name="EXCEPTION_RATE",
                severity=InvariantSeverity.ERROR,
                message=(
                    f"charlie_gate_exception={exceptions}. "
                    "Every gate exception is a contract violation. "
                    "Investigate charlie_gate_exception events in logs."
                ),
                counts={"exceptions": exceptions},
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def check_session_invariants(
    counts: Dict,
    *,
    rejection_ratio_warn: float = _DEFAULT_REJECTION_RATIO_WARN,
    dedup_ratio_max: float = _DEFAULT_DEDUP_RATIO_MAX,
    exception_threshold: int = _DEFAULT_EXCEPTION_THRESHOLD,
) -> List[InvariantViolation]:
    """
    Run all invariant checks against a dict of event counts.

    Parameters
    ----------
    counts:
        Dict mapping event name → count for the current session.
        Expected keys (all optional; missing keys treated as 0):
        - charlie_gate_approved
        - charlie_gate_rejected
        - charlie_gate_exception
        - charlie_gate_dedup_cache_hit
        - order_submitted
        - polymarket_api_unavailable
        - binance_feed_unhealthy

    Returns
    -------
    List of InvariantViolation (empty = all clear).
    Sorted: ERROR first, then WARNING.
    """
    violations: List[InvariantViolation] = []
    violations += _check_order_submission_rate(counts)
    violations += _check_rejection_dominance(counts, rejection_ratio_warn)
    violations += _check_dedup_sanity(counts, dedup_ratio_max)
    violations += _check_feed_health_at_approval(counts)
    violations += _check_exception_rate(counts, exception_threshold)

    # Sort: ERRORs first, then WARNINGs
    violations.sort(key=lambda v: 0 if v.severity == InvariantSeverity.ERROR else 1)
    return violations


def format_violations_for_terminal(violations: List[InvariantViolation]) -> str:
    """Format violations for human-readable terminal/session output."""
    if not violations:
        return "\u2705  All session invariants: OK"

    lines = []
    for v in violations:
        icon = "\u274c" if v.severity == InvariantSeverity.ERROR else "\u26a0\ufe0f"
        lines.append(f"{icon} [{v.severity.name}] {v.name}: {v.message}")
        if v.counts:
            lines.append(f"     counts: {v.counts}")
    return "\n".join(lines)
