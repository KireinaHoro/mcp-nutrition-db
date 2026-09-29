"""Pure chronological energy ledger; no persistence or transport dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import NamedTuple

from .energy import (
    DAILY_ADJUSTMENT_MKCAL,
    DEBIT_EFFECTIVE_FROM,
    EXCEPTIONAL_BURN_MKCAL,
    EXCEPTIONAL_DURATION_MS,
    RECOVERY_WEIGHTS_PERMILLE,
    recovery_reservations,
)


@dataclass(frozen=True)
class GoalFacts:
    base_mkcal: int
    deficit_mkcal: int


@dataclass
class DayFacts:
    intake_mkcal: int = 0
    intake_logged: bool = False
    intake_complete: bool = True
    reported_mkcal: int = 0
    credited_mkcal: int = 0
    duration_ms: int = 0
    planned_exceptional: bool = False


class Reservation(NamedTuple):
    source_date: date
    day_offset: int
    amount_mkcal: int


@dataclass(frozen=True)
class RecoveryAllocation:
    date: date
    day_offset: int
    candidate_mkcal: int
    scheduled_mkcal: int


@dataclass(frozen=True)
class RecoverySource:
    source_date: date
    scheduled_mkcal: int
    protected: bool
    provisional: bool


@dataclass
class LedgerDay:
    date: date
    status: str
    provisional: bool
    has_goal: bool
    base_mkcal: int
    deficit_mkcal: int
    ordinary_mkcal: int
    intake_mkcal: int
    intake_complete: bool
    reported_mkcal: int
    credited_mkcal: int
    incoming_mkcal: int
    incoming_used_mkcal: int | None
    incoming_repaid_mkcal: int
    exercise_used_mkcal: int | None
    unused_exercise_mkcal: int | None
    recovery_pool_cap_mkcal: int | None
    recovery_pool_mkcal: int | None
    exceptional_activity: bool
    intake_logged: bool
    protected_incoming_mkcal: int
    recovery_source_provisional: bool
    incoming_recovery_sources: list[RecoverySource]
    suppressed_recovery_mkcal: int
    opening_debit_mkcal: int
    debit_added_mkcal: int
    debit_repaid_mkcal: int
    closing_debit_mkcal: int
    repayment_from_exercise_mkcal: int
    adjustment_mkcal: int
    pause_reasons: list[str]
    repayment_eligible: bool
    unsettled_dates: list[str]
    recovery_schedule: list[RecoveryAllocation] = field(default_factory=list)


@dataclass
class LedgerState:
    outstanding_mkcal: int = 0
    unsettled_dates: list[str] = field(default_factory=list)
    pending: dict[date, list[Reservation]] = field(default_factory=dict)
    days: dict[date, LedgerDay] = field(default_factory=dict)


def allocate_incoming(day: date, state: LedgerState) -> tuple[list[Reservation], int]:
    allocated = []
    suppressed = 0
    for candidate in state.pending.get(day, []):
        source = state.days[candidate.source_date]
        amount = candidate.amount_mkcal
        if (
            day >= DEBIT_EFFECTIVE_FROM
            and state.outstanding_mkcal > 0
            and not source.exceptional_activity
        ):
            suppressed += amount
            amount = 0
        else:
            allocated.append(candidate)
        source.recovery_schedule.append(
            RecoveryAllocation(day, candidate.day_offset, candidate.amount_mkcal, amount)
        )
    return allocated, suppressed


def recovery_capacity(
    day: date, goals: dict[date, GoalFacts | None], state: LedgerState
) -> list[int]:
    capacities = []
    for offset in range(1, 4):
        destination = day + timedelta(days=offset)
        goal = goals.get(destination)
        cap = 0 if goal is None else goal.deficit_mkcal
        committed = sum(item.amount_mkcal for item in state.pending.get(destination, []))
        capacities.append(max(0, cap - committed))
    return capacities


def calculate_ledger(
    facts: dict[date, DayFacts],
    goals: dict[date, GoalFacts],
    start_date: date,
    end_date: date,
    *,
    today: date,
) -> dict[date, LedgerDay]:
    scan_start = min(start_date, min(goals)) if goals else start_date
    scan_end = end_date + timedelta(days=len(RECOVERY_WEIGHTS_PERMILLE))
    active_goals: dict[date, GoalFacts | None] = {}
    active_goal = None
    day = scan_start
    while day <= scan_end:
        active_goal = goals.get(day, active_goal)
        active_goals[day] = active_goal
        day += timedelta(days=1)
    state = LedgerState()
    day = scan_start
    while day <= scan_end:
        advance_day(day, facts.get(day, DayFacts()), active_goals, today, state)
        day += timedelta(days=1)
    return state.days


def advance_day(
    current_date: date,
    facts: DayFacts,
    goals: dict[date, GoalFacts | None],
    today: date,
    state: LedgerState,
) -> None:
    goal = goals[current_date]
    base_mkcal = 0 if goal is None else goal.base_mkcal
    deficit_mkcal = 0 if goal is None else goal.deficit_mkcal
    ordinary_mkcal = base_mkcal - deficit_mkcal
    debit_active = current_date >= DEBIT_EFFECTIVE_FROM
    opening_debit_mkcal = state.outstanding_mkcal
    allocated, suppressed_recovery_mkcal = allocate_incoming(current_date, state)
    incoming_mkcal = sum(item.amount_mkcal for item in allocated)
    reported_mkcal = facts.reported_mkcal
    credited_mkcal = facts.credited_mkcal
    intake_mkcal = facts.intake_mkcal
    intake_complete = facts.intake_complete
    provisional = current_date >= today
    intake_logged = facts.intake_logged
    exceptional = (
        credited_mkcal >= EXCEPTIONAL_BURN_MKCAL
        or facts.duration_ms >= EXCEPTIONAL_DURATION_MS
        or facts.planned_exceptional
    )
    previous = state.days.get(current_date - timedelta(days=1))
    previous_exceptional = previous is not None and previous.exceptional_activity
    protected_incoming = sum(
        item.amount_mkcal for item in allocated if state.days[item.source_date].exceptional_activity
    )
    pause_reasons = []
    if exceptional:
        pause_reasons.append("exceptional_activity")
    if previous_exceptional:
        pause_reasons.append("day_after_exceptional_activity")
    if protected_incoming:
        pause_reasons.append("protected_recovery")
    if deficit_mkcal == 0:
        pause_reasons.append("no_planned_deficit")
    adjustment_mkcal = (
        min(DAILY_ADJUSTMENT_MKCAL, opening_debit_mkcal, max(0, ordinary_mkcal))
        if debit_active and not pause_reasons and goal is not None
        else 0
    )
    status = "ok"
    exercise_used_mkcal: int | None = None
    unused_exercise_mkcal: int | None = None
    incoming_used_mkcal: int | None = None
    recovery_pool_cap_mkcal: int | None = None
    recovery_pool_mkcal: int | None = None
    recovery_amounts = [0, 0, 0]
    capacities = recovery_capacity(current_date, goals, state)
    if goal is None:
        status = "no_goal"
    elif not intake_complete:
        status = "incomplete_intake"
    else:
        incoming_used_mkcal = min(max(intake_mkcal - ordinary_mkcal, 0), incoming_mkcal)
        exercise_used_mkcal = min(
            max(intake_mkcal - ordinary_mkcal - incoming_mkcal, 0),
            credited_mkcal,
        )
        unused_exercise_mkcal = credited_mkcal - exercise_used_mkcal
        next_goal = goals.get(current_date + timedelta(days=1))
        next_deficit_mkcal = 0 if next_goal is None else next_goal.deficit_mkcal
        recovery_pool_cap_mkcal = next_deficit_mkcal * 1_000 // RECOVERY_WEIGHTS_PERMILLE[0]
        recovery_amounts = recovery_reservations(
            min(unused_exercise_mkcal, recovery_pool_cap_mkcal), capacities
        )
        recovery_pool_mkcal = sum(recovery_amounts)
        # Ordinary carryover yields to outstanding debit. Exceptional recovery is protected.
        if debit_active and opening_debit_mkcal > 0 and not exceptional:
            recovery_pool_mkcal = 0
            recovery_amounts = [0, 0, 0]

    debit_added_mkcal = 0
    debit_repaid_mkcal = 0
    repayment_from_exercise_mkcal = 0
    repayment_from_incoming_mkcal = 0
    repayment_eligible = (
        debit_active
        and current_date < today
        and intake_logged
        and intake_complete
        and goal is not None
    )
    if debit_active and current_date <= today and goal is not None:
        debit_added_mkcal = max(0, intake_mkcal - base_mkcal - credited_mkcal)
        if not intake_logged or not intake_complete or provisional:
            state.unsettled_dates.append(current_date.isoformat())
        if repayment_eligible:
            # Repay unused unadjusted budget, including incoming recovery.
            extra_deficit = max(
                0,
                ordinary_mkcal
                + credited_mkcal
                + incoming_mkcal
                - intake_mkcal
                - (recovery_pool_mkcal or 0),
            )
            debit_repaid_mkcal = min(opening_debit_mkcal, extra_deficit)
            repayment_from_exercise_mkcal = min(
                debit_repaid_mkcal,
                max(0, (unused_exercise_mkcal or 0) - (recovery_pool_mkcal or 0)),
            )
            repayment_from_incoming_mkcal = min(
                debit_repaid_mkcal - repayment_from_exercise_mkcal,
                incoming_mkcal - (incoming_used_mkcal or 0),
            )
            # If this day clears debit, remaining ordinary exercise credit can recover.
            if not exceptional and debit_repaid_mkcal == opening_debit_mkcal:
                recovery_amounts = recovery_reservations(
                    min(
                        max(0, (unused_exercise_mkcal or 0) - repayment_from_exercise_mkcal),
                        recovery_pool_cap_mkcal or 0,
                    ),
                    capacities,
                )
                recovery_pool_mkcal = sum(recovery_amounts)
        state.outstanding_mkcal = opening_debit_mkcal + debit_added_mkcal - debit_repaid_mkcal

    state.days[current_date] = LedgerDay(
        date=current_date,
        status=status,
        provisional=provisional,
        has_goal=goal is not None,
        base_mkcal=base_mkcal,
        deficit_mkcal=deficit_mkcal,
        ordinary_mkcal=ordinary_mkcal,
        intake_mkcal=intake_mkcal,
        intake_complete=intake_complete,
        reported_mkcal=reported_mkcal,
        credited_mkcal=credited_mkcal,
        incoming_mkcal=incoming_mkcal,
        incoming_used_mkcal=incoming_used_mkcal,
        incoming_repaid_mkcal=repayment_from_incoming_mkcal,
        exercise_used_mkcal=exercise_used_mkcal,
        unused_exercise_mkcal=unused_exercise_mkcal,
        recovery_pool_cap_mkcal=recovery_pool_cap_mkcal,
        recovery_pool_mkcal=recovery_pool_mkcal,
        recovery_schedule=[],
        exceptional_activity=exceptional,
        intake_logged=intake_logged,
        protected_incoming_mkcal=protected_incoming,
        recovery_source_provisional=provisional or not intake_complete or not intake_logged,
        incoming_recovery_sources=[
            RecoverySource(
                item.source_date,
                item.amount_mkcal,
                state.days[item.source_date].exceptional_activity,
                state.days[item.source_date].recovery_source_provisional,
            )
            for item in allocated
        ],
        suppressed_recovery_mkcal=suppressed_recovery_mkcal,
        opening_debit_mkcal=opening_debit_mkcal,
        debit_added_mkcal=debit_added_mkcal,
        debit_repaid_mkcal=debit_repaid_mkcal,
        closing_debit_mkcal=state.outstanding_mkcal,
        repayment_from_exercise_mkcal=repayment_from_exercise_mkcal,
        adjustment_mkcal=adjustment_mkcal,
        pause_reasons=pause_reasons,
        repayment_eligible=repayment_eligible,
        unsettled_dates=list(state.unsettled_dates),
    )
    if recovery_pool_mkcal is not None and recovery_pool_mkcal > 0:
        for offset, amount in enumerate(recovery_amounts, start=1):
            state.pending.setdefault(current_date + timedelta(days=offset), []).append(
                Reservation(current_date, offset, amount)
            )
