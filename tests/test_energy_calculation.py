from __future__ import annotations

import copy
import random
from datetime import date, timedelta

from mcp_nutrition_db.energy_calculation import DayFacts, GoalFacts, calculate_ledger


def test_pure_ledger_conserves_credit_and_is_independent_of_query_start():
    randomizer = random.Random(2941)
    start = date(2026, 9, 1)
    goals = {
        start: GoalFacts(2_500_000, 500_000),
        start + timedelta(days=20): GoalFacts(2_700_000, 400_000),
    }
    facts = {
        start + timedelta(days=offset): DayFacts(
            intake_mkcal=randomizer.randrange(1_000_000, 4_000_000),
            intake_logged=offset % 11 != 0,
            intake_complete=offset % 7 != 0,
            reported_mkcal=burn,
            credited_mkcal=burn,
            duration_ms=randomizer.randrange(0, 240) * 60_000,
            planned_exceptional=offset % 13 == 0,
        )
        for offset in range(50)
        for burn in [randomizer.randrange(0, 3_000_000)]
    }
    # Missing intake days have no observed calorie contribution.
    for fact in facts.values():
        if not fact.intake_logged:
            fact.intake_mkcal = 0
    before = copy.deepcopy((facts, goals))
    end = start + timedelta(days=49)
    today = start + timedelta(days=45)
    ledger = calculate_ledger(facts, goals, start, end, today=today)
    later = calculate_ledger(facts, goals, start + timedelta(days=30), end, today=today)
    assert (facts, goals) == before
    assert ledger == later
    for day, result in ledger.items():
        assert 0 <= result.debit_repaid_mkcal <= result.opening_debit_mkcal
        assert result.closing_debit_mkcal == (
            result.opening_debit_mkcal + result.debit_added_mkcal - result.debit_repaid_mkcal
        )
        assert result.incoming_mkcal <= result.deficit_mkcal
        if day >= today or not result.intake_logged or not result.intake_complete:
            assert result.debit_repaid_mkcal == 0
        if result.unused_exercise_mkcal is not None and day <= end:
            scheduled = sum(item.scheduled_mkcal for item in result.recovery_schedule)
            expired = (
                result.unused_exercise_mkcal - scheduled - result.repayment_from_exercise_mkcal
            )
            assert expired >= 0
            assert scheduled <= (result.recovery_pool_mkcal or 0)
