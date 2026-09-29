"""Active energy-policy planning parameters."""

from __future__ import annotations

from datetime import date
from importlib.resources import files
from typing import Any

POLICY_ID = "energy-credit/v6"
DEBIT_EFFECTIVE_FROM = date(2026, 9, 9)
DAILY_ADJUSTMENT_MKCAL = 200_000
EXCEPTIONAL_BURN_MKCAL = 1_000_000
EXCEPTIONAL_DURATION_MS = 180 * 60_000
REVIEW_AFTER_ELIGIBLE_DAYS = 28


CONFIDENCE_MULTIPLIERS_PERMILLE = {"high": 1_000, "medium": 800, "low": 600}
RECOVERY_WEIGHTS_PERMILLE = (500, 300, 200)


def credited_burn(reported_mkcal: int, confidence: str) -> int:
    return (reported_mkcal * CONFIDENCE_MULTIPLIERS_PERMILLE[confidence] + 500) // 1_000


def energy_policy() -> dict[str, Any]:
    return {
        "policy_id": POLICY_ID,
        "status": "active",
        "calculation_basis": "current_policy",
        "confidence_multipliers": {
            key: value / 1_000 for key, value in CONFIDENCE_MULTIPLIERS_PERMILLE.items()
        },
        "recovery_weights": [value / 1_000 for value in RECOVERY_WEIGHTS_PERMILLE],
        "recovery_pool_cap": "next_day_planned_deficit / first_recovery_weight",
        "recovery_pool_overflow": "repay_eligible_debit_then_expire",
        "daily_cap": "destination_planned_deficit",
        "collision_handling": "earlier_source_reservations_first",
        "reservation": "clip_candidates_to_remaining_destination_capacity_before_repayment",
        "overflow": "repay_eligible_source_debit_then_expire",
        "missed_allocation": "repay_opening_debit_on_settlement_then_expire",
        "ordinary_target_formula": "base_burn - deficit",
        "planned_baseline_formula": "ordinary_target + incoming_recovery - debit_adjustment",
        "available_ceiling_formula": ("planned_baseline + credited_training_burn"),
        "allowance_semantics": "optional_ceiling_not_intake_recommendation",
        "attribution_order": [
            "ordinary_target",
            "incoming_recovery",
            "same_day_exercise_allowance",
        ],
        "debit": {
            "effective_from": DEBIT_EFFECTIVE_FROM.isoformat(),
            "creation": "max(0, logged_intake - base_burn - credited_training_burn)",
            "missed_ordinary_deficit": "forgiven",
            "adjustment_start": "next_day_with_opening_debit_unless_activity_or_recovery_pause",
            "repeat_overshoots": "add_surplus_to_debit; never_stack_daily_adjustments",
            "daily_additional_deficit_cap_kcal": DAILY_ADJUSTMENT_MKCAL / 1_000,
            "balance_cap": None,
            "expiry": None,
            "repayment": "unused_unadjusted_budget_after_source_recovery_reserve",
            "repayment_formula": (
                "min(opening_debit, max(0, ordinary_target + incoming_recovery + "
                "credited_exercise - intake - reserved_source_pool))"
            ),
            "repayment_daily_cap": None,
            "repayment_requires": "past_local_day; at_least_one_intake_entry; known_calories",
            "unsettled_surplus": "accrue_as_provisional_lower_bound",
            "projection": (
                f"ceil(remaining_debit / {DAILY_ADJUSTMENT_MKCAL // 1_000}) "
                "eligible days, not a deadline"
            ),
            "review_after_projected_eligible_days": REVIEW_AFTER_ELIGIBLE_DAYS,
            "pauses": [
                "exceptional_activity",
                "day_after_exceptional_activity",
                "protected_recovery",
            ],
            "pause_effect": "no_additional_restriction; actual_extra_deficit_can_still_repay",
            "pauses_apply_to": "additional_deficit_only; not_actual_repayment",
        },
        "exceptional_activity": {
            "credited_burn_threshold_kcal": EXCEPTIONAL_BURN_MKCAL / 1_000,
            "total_duration_threshold_minutes": EXCEPTIONAL_DURATION_MS / 60_000,
            "explicit_activity_plan_supported": True,
            "recovery_priority": "reserve_capped_pool_before_debit_repayment",
            "unused_budget_after_reserve": "repays_opening_debit_on_settlement_then_expires",
            "repayment_exempt": False,
            "ordinary_carryover_with_debit": "suspended",
            "protected_carryover_with_debit": "honoured",
            "coefficients": "planning_conventions_not_physiological_requirements",
        },
        "day_settlement": {
            "timing": "past_local_calendar_days_on_query; no_timer_or_confirmation",
            "basis": "current_logged_intake; empty_or_unknown_calorie_days_cannot_repay",
            "corrections": (
                "backdated_additions_edits_and_deletions_recalculate_subsequent_balances"
            ),
        },
        "document_ref": "docs/energy-credit-policy.md",
        "policy_text": files("mcp_nutrition_db").joinpath("energy-credit-policy.md").read_text(),
    }


def recovery_reservations(pool_mkcal: int, capacity_mkcal: list[int]) -> list[int]:
    """Fit each tapered candidate into capacity left by earlier source days."""
    first = (pool_mkcal * RECOVERY_WEIGHTS_PERMILLE[0] + 500) // 1_000
    second = (pool_mkcal * RECOVERY_WEIGHTS_PERMILLE[1] + 500) // 1_000
    candidates = (first, second, pool_mkcal - first - second)
    return [
        min(amount, capacity) for amount, capacity in zip(candidates, capacity_mkcal, strict=True)
    ]
