"""Serialize typed ledger results into the stable public energy contract."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .energy import (
    DAILY_ADJUSTMENT_MKCAL,
    DEBIT_EFFECTIVE_FROM,
    POLICY_ID,
    REVIEW_AFTER_ELIGIBLE_DAYS,
)
from .energy_calculation import LedgerDay
from .nutrients import unscale as _unscale


def serialize_balances(
    days: dict[date, LedgerDay], start_date: date, end_date: date, timezone: str, today: date
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    current_date = start_date
    while current_date <= end_date:
        item = days[current_date]
        schedule = sorted(item.recovery_schedule, key=lambda value: value.day_offset)
        scheduled_total = sum(value.scheduled_mkcal for value in schedule)
        unused_exercise = item.unused_exercise_mkcal
        recovery_pool = item.recovery_pool_mkcal
        incoming_remaining = (
            None
            if item.incoming_used_mkcal is None
            else item.incoming_mkcal - item.incoming_used_mkcal
        )
        results[current_date.isoformat()] = {
            "policy_id": POLICY_ID,
            "calculation_basis": "current_policy",
            "date": current_date.isoformat(),
            "timezone": timezone,
            "status": item.status,
            "provisional": item.provisional,
            "base_burn_kcal": _unscale(item.base_mkcal, 1_000),
            "deficit_kcal": _unscale(item.deficit_mkcal, 1_000),
            "ordinary_target_kcal": _unscale(item.ordinary_mkcal, 1_000),
            "intake_kcal": _unscale(item.intake_mkcal, 1_000),
            "intake_complete": item.intake_complete,
            "day_closed": not item.provisional,
            "intake_logged": item.intake_logged,
            "intake_settled": not item.recovery_source_provisional,
            "exceptional_activity": item.exceptional_activity,
            "protected_incoming_recovery_kcal": _unscale(item.protected_incoming_mkcal, 1_000),
            "suppressed_recovery_kcal": _unscale(item.suppressed_recovery_mkcal, 1_000),
            "debit": {
                "active": current_date >= DEBIT_EFFECTIVE_FROM,
                "opening_kcal": _unscale(item.opening_debit_mkcal, 1_000),
                "added_kcal": _unscale(item.debit_added_mkcal, 1_000),
                "repaid_kcal": _unscale(item.debit_repaid_mkcal, 1_000),
                "closing_kcal": _unscale(item.closing_debit_mkcal, 1_000),
                "additional_deficit_kcal": _unscale(item.adjustment_mkcal, 1_000),
                "pause_reasons": item.pause_reasons,
                "repayment_eligible": item.repayment_eligible,
                "balance_provisional": bool(item.unsettled_dates),
                "unsettled_dates": item.unsettled_dates,
                "projected_eligible_days": (item.closing_debit_mkcal + DAILY_ADJUSTMENT_MKCAL - 1)
                // DAILY_ADJUSTMENT_MKCAL,
                "review_recommended": item.closing_debit_mkcal
                > (REVIEW_AFTER_ELIGIBLE_DAYS * DAILY_ADJUSTMENT_MKCAL),
            },
            "reported_training_burn_kcal": _unscale(item.reported_mkcal, 1_000),
            "credited_training_burn_kcal": _unscale(item.credited_mkcal, 1_000),
            "incoming_recovery_kcal": _unscale(item.incoming_mkcal, 1_000),
            "incoming_recovery_sources": [
                {
                    "source_date": source.source_date.isoformat(),
                    "scheduled_kcal": _unscale(source.scheduled_mkcal, 1_000),
                    "protected": source.protected,
                    "provisional": source.provisional,
                }
                for source in item.incoming_recovery_sources
            ],
            "incoming_recovery_used_kcal": _unscale(item.incoming_used_mkcal, 1_000),
            "incoming_recovery_remaining_kcal": _unscale(incoming_remaining, 1_000),
            "incoming_recovery_repaid_kcal": _unscale(item.incoming_repaid_mkcal, 1_000),
            "incoming_recovery_expired_kcal": (
                _unscale(
                    None
                    if incoming_remaining is None
                    else incoming_remaining - item.incoming_repaid_mkcal,
                    1_000,
                )
                if current_date < today
                else None
            ),
            "planned_baseline_kcal": (
                None
                if not item.has_goal
                else _unscale(
                    item.ordinary_mkcal + item.incoming_mkcal - item.adjustment_mkcal,
                    1_000,
                )
            ),
            "available_ceiling_kcal": (
                None
                if not item.has_goal
                else _unscale(
                    item.ordinary_mkcal
                    + item.incoming_mkcal
                    + item.credited_mkcal
                    - item.adjustment_mkcal,
                    1_000,
                )
            ),
            "exercise_credit_used_kcal": _unscale(item.exercise_used_mkcal, 1_000),
            "unused_exercise_credit_kcal": _unscale(unused_exercise, 1_000),
            "recovery_pool_cap_kcal": _unscale(item.recovery_pool_cap_mkcal, 1_000),
            "recovery_pool_kcal": _unscale(recovery_pool, 1_000),
            "exercise_credit_excluded_from_recovery_kcal": (
                None
                if unused_exercise is None or recovery_pool is None
                else _unscale(unused_exercise - recovery_pool, 1_000)
            ),
            "recovery_schedule": [
                {
                    "date": value.date.isoformat(),
                    "day_offset": value.day_offset,
                    "candidate_kcal": _unscale(value.candidate_mkcal, 1_000),
                    "provisional": item.recovery_source_provisional,
                    "protected": item.exceptional_activity,
                    "scheduled_kcal": _unscale(value.scheduled_mkcal, 1_000),
                    "expired_kcal": _unscale(value.candidate_mkcal - value.scheduled_mkcal, 1_000),
                }
                for value in schedule
            ],
            "recovery_scheduled_kcal": _unscale(scheduled_total, 1_000),
            "recovery_pool_expired_at_creation_kcal": (
                None if recovery_pool is None else _unscale(recovery_pool - scheduled_total, 1_000)
            ),
            "exercise_credit_used_for_debit_kcal": _unscale(
                item.repayment_from_exercise_mkcal, 1_000
            ),
            "exercise_credit_expired_at_creation_kcal": (
                None
                if unused_exercise is None
                else _unscale(
                    unused_exercise - scheduled_total - item.repayment_from_exercise_mkcal,
                    1_000,
                )
            ),
        }
        current_date += timedelta(days=1)
    return results
