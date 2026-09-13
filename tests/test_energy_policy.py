from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from mcp_nutrition_db.models import (
    ActivityPlanInput,
    EntryChanges,
    GoalInput,
    LogEntryInput,
    LogTrainingInput,
    TrainingChanges,
)
from mcp_nutrition_db.repository import NutritionRepository, RevisionConflictError


@pytest.fixture
def repo(tmp_path: Path) -> NutritionRepository:
    repository = NutritionRepository(
        tmp_path / "energy.sqlite3", clock=lambda: datetime(2026, 10, 1, 12, tzinfo=UTC)
    )
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2500,
            deficit_kcal=500,
            reason="Test baseline",
        )
    )
    return repository


def food(repo: NutritionRepository, day: str, kcal: float, title: str = "Meal") -> dict[str, Any]:
    return repo.create_entry(
        LogEntryInput.model_validate(
            {
                "occurred_at": day + "T18:00:00+02:00",
                "kind": "dinner",
                "title": title,
                "components": [
                    {
                        "name": "Food",
                        "source": {"type": "user_provided"},
                        "nutrition": {"calories_kcal": kcal},
                    }
                ],
            }
        )
    )


def training(
    repo: NutritionRepository,
    day: str,
    burn: float = 3917,
    duration: float = 420.7,
    confidence: str = "medium",
) -> dict[str, Any]:
    return repo.create_training(
        LogTrainingInput.model_validate(
            {
                "occurred_at": day + "T08:00:00+02:00",
                "activity": "Hike",
                "duration_minutes": duration,
                "reported_burn_kcal": burn,
                "confidence": confidence,
                "measurement_method": "heart_rate_gps_model",
                "source": {"type": "wearable"},
            }
        )
    )


def balance(repo: NutritionRepository, day: str) -> dict[str, Any]:
    return repo.energy_balance(date.fromisoformat(day))


def test_current_trip_debit_applies_without_context_or_personal_records(
    repo: NutritionRepository,
) -> None:
    food(repo, "2026-09-09", 5307.1)
    result = balance(repo, "2026-09-10")
    assert result["debit"]["opening_kcal"] == 2807.1
    assert result["debit"]["additional_deficit_kcal"] == 200
    assert result["debit"]["balance_provisional"] is True
    assert result["debit"]["pause_reasons"] == []
    assert "travel_context" not in result
    with sqlite3.connect(repo.database_path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "trips" not in tables
        assert connection.execute("SELECT count(*) FROM day_reviews").fetchone()[0] == 0


def test_repeated_overshoots_extend_duration_without_stacking_or_questions(
    repo: NutritionRepository,
) -> None:
    for day, kcal in [("2026-09-09", 5300), ("2026-09-10", 3200), ("2026-09-11", 3500)]:
        food(repo, day, kcal, "Travel meal")
    result = balance(repo, "2026-09-12")
    assert result["debit"]["opening_kcal"] == 4500
    assert result["debit"]["projected_eligible_days"] == 23
    assert result["debit"]["additional_deficit_kcal"] == 200
    assert result["planned_baseline_kcal"] == 1800
    assert result["debit"]["pause_reasons"] == []
    food(repo, "2026-09-12", 1800)
    assert balance(repo, "2026-09-12")["debit"]["closing_kcal"] == 4300


def test_meal_details_do_not_change_accounting(
    repo: NutritionRepository,
) -> None:
    entry = food(repo, "2026-09-09", 5300, "Inflight meal; away for two weeks")
    initial = balance(repo, "2026-09-10")
    repo.update_entry(
        entry["entry_id"],
        expected_revision=1,
        reason="Correct title",
        changes=EntryChanges(title="Dinner at home"),
    )
    assert balance(repo, "2026-09-10") == initial


def test_actual_repayment_not_target_or_ordinary_deficit_and_no_expiry(
    repo: NutritionRepository,
) -> None:
    food(repo, "2026-09-09", 5300)
    food(repo, "2026-09-10", 2000)
    food(repo, "2026-09-11", 1800)
    assert balance(repo, "2026-09-10")["debit"]["repaid_kcal"] == 0
    result = balance(repo, "2026-09-11")
    assert result["planned_baseline_kcal"] == 1800
    assert result["debit"]["repaid_kcal"] == 200
    assert result["debit"]["closing_kcal"] == 2600
    assert result["debit"]["projected_eligible_days"] == 13
    # Empty and future days never repay; neither elapsed time nor a query changes the balance.
    assert balance(repo, "2026-11-01")["debit"]["closing_kcal"] == 2600
    assert balance(repo, "2026-09-11") == result


def test_local_midnight_settles_without_event_or_confirmation(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 5300)
    food(repo, "2026-09-10", 1800)
    repo.clock = lambda: datetime(2026, 9, 10, 21, 59, 59, tzinfo=UTC)
    result = balance(repo, "2026-09-10")
    assert result["day_closed"] is False
    assert result["intake_settled"] is False
    assert result["debit"]["repaid_kcal"] == 0
    repo.clock = lambda: datetime(2026, 9, 10, 22, tzinfo=UTC)
    result = balance(repo, "2026-09-10")
    assert result["day_closed"] is True
    assert result["intake_settled"] is True
    assert result["debit"]["repaid_kcal"] == 200
    assert result["debit"]["unsettled_dates"] == []
    assert result["debit"]["balance_provisional"] is False


def test_backdated_meals_edits_moves_and_deletions_recalculate(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 5300)
    food(repo, "2026-09-10", 1800)
    food(repo, "2026-09-11", 1800)
    assert balance(repo, "2026-09-12")["debit"]["opening_kcal"] == 2400
    late = food(repo, "2026-09-10", 400)
    assert balance(repo, "2026-09-12")["debit"]["opening_kcal"] == 2600
    repo.update_entry(
        late["entry_id"],
        expected_revision=1,
        reason="Correct amount",
        changes=EntryChanges.model_validate(
            {
                "components": [
                    {
                        "name": "Food",
                        "source": {"type": "user_provided"},
                        "nutrition": {"calories_kcal": 1000},
                    }
                ]
            }
        ),
    )
    assert balance(repo, "2026-09-12")["debit"]["opening_kcal"] == 2900
    repo.update_entry(
        late["entry_id"],
        expected_revision=2,
        reason="Correct date",
        changes=EntryChanges(occurred_at=datetime.fromisoformat("2026-09-09T18:00:00+02:00")),
    )
    assert balance(repo, "2026-09-12")["debit"]["opening_kcal"] == 3400
    repo.delete_entry(late["entry_id"], expected_revision=3, reason="Duplicate")
    assert balance(repo, "2026-09-12")["debit"]["opening_kcal"] == 2400


def test_legacy_confirmation_cannot_settle_empty_day(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 5300)
    repo.set_activity_plan(
        ActivityPlanInput(
            on_date=date(2026, 9, 10),
            exceptional_activity=False,
            reason="No planned activity",
        )
    )
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("UPDATE day_reviews SET intake_complete = 1")
    empty = balance(repo, "2026-09-10")
    assert empty["debit"]["repaid_kcal"] == 0
    assert empty["intake_logged"] is False
    food(repo, "2026-09-10", 1800)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("UPDATE day_reviews SET intake_complete = 0")
    assert balance(repo, "2026-09-10")["debit"]["repaid_kcal"] == 200
    plan = repo.get_activity_plan(date(2026, 9, 10))["activity_plan"]
    assert "intake_complete" not in plan
    with pytest.raises(RevisionConflictError):
        repo.set_activity_plan(
            ActivityPlanInput(
                on_date=date(2026, 9, 10),
                exceptional_activity=True,
                reason="Stale change",
            )
        )
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM day_review_revisions").fetchone()[0] == 1


def test_hike_reserves_recovery_before_repaying_and_pauses_restriction(
    repo: NutritionRepository,
) -> None:
    food(repo, "2026-09-09", 5300)
    food(repo, "2026-09-12", 2455.94)
    training(repo, "2026-09-12")
    result = balance(repo, "2026-09-12")
    assert result["credited_training_burn_kcal"] == 3133.6
    assert result["debit"]["additional_deficit_kcal"] == 0
    assert result["recovery_pool_kcal"] == 1000
    assert result["debit"]["repaid_kcal"] == 1677.66
    assert result["debit"]["closing_kcal"] == 1122.34
    assert result["exercise_credit_used_for_debit_kcal"] == 1677.66
    assert result["exercise_credit_expired_at_creation_kcal"] == 0
    assert [item["scheduled_kcal"] for item in result["recovery_schedule"]] == [500, 300, 200]
    for day, recovery in [(13, 500), (14, 300), (15, 200)]:
        result = balance(repo, f"2026-09-{day}")
        assert result["incoming_recovery_kcal"] == recovery
        assert result["debit"]["additional_deficit_kcal"] == 0
    assert balance(repo, "2026-09-16")["debit"]["additional_deficit_kcal"] == 200


def test_recovery_cannot_repay_debit_twice_and_clipped_reserve_expires(
    repo: NutritionRepository,
) -> None:
    food(repo, "2026-09-09", 5300)
    for day in ["2026-09-12", "2026-09-13"]:
        training(repo, day)
        food(repo, day, 2455.94)
    hike = balance(repo, "2026-09-12")
    assert hike["debit"]["repaid_kcal"] == 1677.66
    assert hike["recovery_pool_expired_at_creation_kcal"] > 0
    for day in range(12, 17):
        result = balance(repo, f"2026-09-{day}")
        assert result["incoming_recovery_kcal"] <= 500
        assert result["exercise_credit_expired_at_creation_kcal"] >= 0
        assert result["unused_exercise_credit_kcal"] == pytest.approx(
            result["recovery_scheduled_kcal"]
            + result["exercise_credit_used_for_debit_kcal"]
            + result["exercise_credit_expired_at_creation_kcal"]
        )
    # Spending a protected allowance cannot recreate debit or repay it merely by expiring it.
    food(repo, "2026-09-14", 2500)
    assert balance(repo, "2026-09-14")["debit"]["added_kcal"] == 0
    assert balance(repo, "2026-09-14")["debit"]["repaid_kcal"] == 0


def test_ordinary_exercise_pays_debit_before_carryover(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 2700)
    training(repo, "2026-09-10", 600, 60, "high")
    food(repo, "2026-09-10", 2000)
    result = balance(repo, "2026-09-10")
    assert result["debit"]["repaid_kcal"] == 200
    assert result["debit"]["closing_kcal"] == 0
    assert result["recovery_pool_kcal"] == 400
    assert result["recovery_scheduled_kcal"] == 400
    assert result["exercise_credit_expired_at_creation_kcal"] == 0


def test_corrections_recalculate_future_debit_and_protected_recovery(
    repo: NutritionRepository,
) -> None:
    entry = food(repo, "2026-09-09", 5300)
    hike = training(repo, "2026-09-12")
    food(repo, "2026-09-12", 2455.94)
    assert balance(repo, "2026-09-13")["debit"]["opening_kcal"] == 1122.34
    repo.update_training(
        hike["training_id"],
        expected_revision=1,
        reason="Correct burn",
        changes=TrainingChanges(reported_burn_kcal=3000),
    )
    assert balance(repo, "2026-09-13")["debit"]["opening_kcal"] == 1855.94
    repo.delete_entry(entry["entry_id"], expected_revision=1, reason="Duplicate")
    assert balance(repo, "2026-09-13")["debit"]["opening_kcal"] == 0
    assert balance(repo, "2026-09-13")["incoming_recovery_kcal"] == 500


def test_planned_activity_pauses_before_training_is_logged(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 5300)
    repo.set_activity_plan(
        ActivityPlanInput(
            on_date=date(2026, 10, 3),
            exceptional_activity=True,
            reason="Planned seven hour hike",
        )
    )
    assert balance(repo, "2026-10-03")["debit"]["additional_deficit_kcal"] == 0
    assert balance(repo, "2026-10-04")["debit"]["additional_deficit_kcal"] == 0
    assert balance(repo, "2026-10-05")["debit"]["additional_deficit_kcal"] == 200


def test_last_partial_payment_and_missed_deficits_are_forgiven(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 2600)
    food(repo, "2026-09-10", 2200)
    assert balance(repo, "2026-09-10")["debit"]["closing_kcal"] == 100
    assert balance(repo, "2026-09-11")["planned_baseline_kcal"] == 1900
    food(repo, "2026-09-11", 1900)
    assert balance(repo, "2026-09-11")["debit"]["closing_kcal"] == 0


def test_effective_date_and_no_references_to_other_policies(repo: NutritionRepository) -> None:
    food(repo, "2026-09-08", 10000)
    assert balance(repo, "2026-09-09")["debit"]["opening_kcal"] == 0
    policy = json.dumps(repo.energy_policy())
    assert "energy-credit/v5" in policy
    assert all(version not in policy for version in ["v1", "v2", "v3"])


def test_day_with_unknown_calories_does_not_repay(repo: NutritionRepository) -> None:
    food(repo, "2026-09-09", 5300)
    entry = food(repo, "2026-09-10", 1800)
    repo.update_entry(
        entry["entry_id"],
        expected_revision=1,
        reason="Calories unknown",
        changes=EntryChanges.model_validate(
            {
                "components": [
                    {
                        "name": "Food",
                        "source": {"type": "estimated"},
                        "nutrition": {"protein_g": 50},
                    }
                ]
            }
        ),
    )
    result = balance(repo, "2026-09-10")
    assert result["status"] == "incomplete_intake"
    assert result["debit"]["repaid_kcal"] == 0
    assert result["intake_settled"] is False
    repo.update_entry(
        entry["entry_id"],
        expected_revision=2,
        reason="Calories supplied",
        changes=EntryChanges.model_validate(
            {
                "components": [
                    {
                        "name": "Food",
                        "source": {"type": "user_provided"},
                        "nutrition": {"calories_kcal": 1800},
                    }
                ]
            }
        ),
    )
    assert balance(repo, "2026-09-10")["debit"]["repaid_kcal"] == 200
    repo.delete_entry(entry["entry_id"], expected_revision=3, reason="Wrong entry")
    assert balance(repo, "2026-09-10")["debit"]["repaid_kcal"] == 0


@pytest.mark.parametrize("intake, repaid", [(2500, 0), (2300, 200), (2000, 500), (1900, 600)])
def test_unused_recovery_repays_after_midnight(
    repo: NutritionRepository, intake: float, repaid: float
) -> None:
    food(repo, "2026-09-09", 5300)
    training(repo, "2026-09-12")
    food(repo, "2026-09-12", 2455.94)
    food(repo, "2026-09-13", intake)
    repo.clock = lambda: datetime(2026, 9, 13, 21, 59, tzinfo=UTC)
    assert balance(repo, "2026-09-13")["debit"]["repaid_kcal"] == 0
    repo.clock = lambda: datetime(2026, 9, 13, 22, tzinfo=UTC)
    result = balance(repo, "2026-09-13")
    assert result["debit"]["additional_deficit_kcal"] == 0
    assert result["debit"]["repaid_kcal"] == repaid
    assert result["debit"]["closing_kcal"] == pytest.approx(1122.34 - repaid)
    assert result["incoming_recovery_repaid_kcal"] == min(repaid, 500)
    assert result["incoming_recovery_expired_kcal"] == 0
    assert balance(repo, "2026-09-12")["debit"]["repaid_kcal"] == 1677.66
    late = food(repo, "2026-09-13", 700)
    assert balance(repo, "2026-09-13")["debit"]["repaid_kcal"] == 0
    repo.delete_entry(late["entry_id"], expected_revision=1, reason="Duplicate")
    assert balance(repo, "2026-09-13")["debit"]["repaid_kcal"] == repaid


def test_recovery_repayment_capped_at_debit_and_remainder_expires(
    repo: NutritionRepository,
) -> None:
    food(repo, "2026-09-09", 4300)
    training(repo, "2026-09-12")
    food(repo, "2026-09-12", 2455.94)
    food(repo, "2026-09-13", 2000)
    result = balance(repo, "2026-09-13")
    assert result["debit"]["repaid_kcal"] == 122.34
    assert result["debit"]["closing_kcal"] == 0
    assert result["incoming_recovery_repaid_kcal"] == 122.34
    assert result["incoming_recovery_expired_kcal"] == 377.66


def test_served_policy_is_complete_and_matches_document(repo: NutritionRepository) -> None:
    policy = repo.energy_policy()
    document = Path(__file__).parents[1] / "docs/energy-credit-policy.md"
    assert policy["policy_text"] == document.read_text()
    assert "unadjusted_budget =" in policy["policy_text"]
    assert all(f"energy-credit/v{version}" not in json.dumps(policy) for version in range(1, 5))
