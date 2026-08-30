from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from mcp_nutrition_db.models import (
    Confidence,
    EntryChanges,
    GoalInput,
    ListEntriesInput,
    ListTrainingsInput,
    LogEntryInput,
    LogTrainingInput,
    NutritionValues,
    RelativeDayWindow,
    SummarizeInput,
    TrainingChanges,
    TrainingSource,
)
from mcp_nutrition_db.repository import (
    MIGRATION_1,
    MIGRATION_2,
    NotFoundError,
    NutritionRepository,
    RevisionConflictError,
)


def today() -> RelativeDayWindow:
    return RelativeDayWindow(type="relative_day", day="today", timezone="Europe/Zurich")


def calorie_entry(on_date: date, calories: float) -> LogEntryInput:
    return LogEntryInput.model_validate(
        {
            "occurred_at": f"{on_date.isoformat()}T12:00:00+02:00",
            "kind": "lunch",
            "title": "Energy accounting fixture",
            "components": [
                {
                    "name": "Complete calorie fixture",
                    "source": {"type": "user_provided", "detail": "Test fixture"},
                    "nutrition": {"calories_kcal": calories},
                }
            ],
        }
    )


def test_schema_migration_is_repeatable(repository: NutritionRepository) -> None:
    assert repository.schema_version() == 3
    repository.migrate()
    assert repository.schema_version() == 3


def test_v1_goal_migrates_calorie_target_to_base_burn(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executescript(MIGRATION_1)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (1, '2026-08-01T00:00:00Z')"
        )
        connection.execute(
            """
            INSERT INTO daily_goals(
                goal_id, effective_from, timezone, calories_mkcal, reason, created_at, updated_at
            ) VALUES ('legacy', '2026-08-01', 'Europe/Zurich', 2000000, 'Legacy',
                      '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """
        )

    migrated = NutritionRepository(database)
    goal = migrated.get_goals(on_date=date(2026, 8, 27))["current"]
    assert migrated.schema_version() == 3
    assert goal["base_burn_kcal"] == 2_000
    assert goal["targets"]["calories_kcal"] is None
    assert goal["energy_budget"]["ordinary_target_kcal"] == 2_000
    assert goal["energy_budget"]["policy_id"] == "energy-credit/v2"


def test_v2_training_migrates_with_conservative_provenance(tmp_path: Path) -> None:
    database = tmp_path / "legacy-training.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.executescript(MIGRATION_1)
        connection.executescript(MIGRATION_2)
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(1, "2026-08-01T00:00:00Z"), (2, "2026-08-02T00:00:00Z")],
        )
        connection.execute(
            """
            INSERT INTO trainings(
                training_id, revision, occurred_at, occurred_at_utc, timezone,
                activity, duration_milliseconds, calories_burned_mkcal,
                source_type, source_detail, created_at, updated_at
            ) VALUES (
                'legacy-training', 1, '2026-08-27T18:00:00+02:00',
                '2026-08-27T16:00:00Z', 'Europe/Zurich', 'Legacy ride', 3600000,
                1000000, 'wearable', 'Legacy watch', '2026-08-27T18:00:00Z',
                '2026-08-27T18:00:00Z'
            )
            """
        )

    migrated = NutritionRepository(database)
    training = migrated.get_training("legacy-training")
    assert migrated.schema_version() == 3
    assert training["reported_burn_kcal"] == 1_000
    assert training["confidence"] == "medium"
    assert training["measurement_method"] == "device_estimate"
    assert training["credited_burn_kcal"] == 800


def test_create_exact_retry_and_force_new(repository: NutritionRepository, meal: object) -> None:
    first = repository.create_entry(meal)  # type: ignore[arg-type]
    retry = repository.create_entry(meal)  # type: ignore[arg-type]

    assert retry["entry_id"] == first["entry_id"]
    assert retry["deduplicated"] is True
    assert first["totals"]["calories_kcal"] == 484
    assert first["completeness"]["carbohydrate_g"] == {
        "known_components": 1,
        "total_components": 2,
        "complete": False,
    }
    assert first["components"][0]["source"]["type"] == "estimated"

    forced_request = meal.model_copy(update={"force_new": True})  # type: ignore[attr-defined]
    forced = repository.create_entry(forced_request)
    assert forced["entry_id"] != first["entry_id"]
    assert forced["deduplicated"] is False


def test_update_audit_conflict_and_delete(repository: NutritionRepository, meal: object) -> None:
    created = repository.create_entry(meal)  # type: ignore[arg-type]
    updated = repository.update_entry(
        created["entry_id"],
        1,
        "User corrected the title",
        EntryChanges(title="Large salmon rice bowl"),
    )

    assert updated["revision"] == 2
    assert updated["title"] == "Large salmon rice bowl"
    history = repository.revision_history(created["entry_id"])
    assert history[0]["snapshot"]["title"] == "Rice bowl with salmon"
    assert history[0]["operation"] == "update"

    with pytest.raises(RevisionConflictError) as conflict:
        repository.update_entry(
            created["entry_id"], 1, "Stale correction", EntryChanges(notes="wrong")
        )
    assert conflict.value.current == 2

    deleted = repository.delete_entry(created["entry_id"], 2, "Logged in error")
    assert deleted == {"entry_id": created["entry_id"], "revision": 3, "deleted": True}
    with pytest.raises(NotFoundError):
        repository.get_entry(created["entry_id"])
    assert repository.revision_history(created["entry_id"])[-1]["operation"] == "delete"


def test_list_today_summary_and_pagination(repository: NutritionRepository, meal: object) -> None:
    first = repository.create_entry(meal)  # type: ignore[arg-type]
    second_request = meal.model_copy(  # type: ignore[attr-defined]
        update={
            "title": "Afternoon repeat",
            "occurred_at": meal.occurred_at.replace(hour=15),  # type: ignore[attr-defined]
        }
    )
    second = repository.create_entry(second_request)

    page_one = repository.list_entries(ListEntriesInput(window=today(), limit=1))
    assert [entry["entry_id"] for entry in page_one["entries"]] == [second["entry_id"]]
    assert page_one["next_cursor"] is not None
    assert page_one["resolved_window"]["start"] == "2026-08-26T22:00:00Z"

    page_two = repository.list_entries(
        ListEntriesInput(window=today(), limit=1, cursor=page_one["next_cursor"])
    )
    assert [entry["entry_id"] for entry in page_two["entries"]] == [first["entry_id"]]

    summary = repository.summarize(SummarizeInput(window=today(), grouping="whole_range"))
    assert summary["groups"][0]["entry_count"] == 2
    assert summary["groups"][0]["totals"]["calories_kcal"] == 968
    assert summary["groups"][0]["completeness"]["carbohydrate_g"]["complete"] is True


def test_effective_dated_goals(repository: NutritionRepository) -> None:
    august = repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_000,
            targets=NutritionValues(protein_g=120),
            reason="Initial goal",
        )
    )
    september = repository.set_goals(
        GoalInput(
            effective_from=date(2026, 9, 1),
            base_burn_kcal=2_200,
            targets=NutritionValues(protein_g=130),
            reason="Training block",
        )
    )

    assert (
        repository.get_goals(on_date=date(2026, 8, 27))["current"]["goal_id"] == august["goal_id"]
    )
    assert (
        repository.get_goals(on_date=date(2026, 9, 3))["current"]["goal_id"] == september["goal_id"]
    )

    replacement = repository.set_goals(
        GoalInput(
            effective_from=date(2026, 9, 1),
            base_burn_kcal=2_100,
            targets=NutritionValues(protein_g=125),
            reason="Corrected target",
        )
    )
    assert replacement["goal_id"] == september["goal_id"]
    assert replacement["base_burn_kcal"] == 2_100
    assert len(repository.get_goals(on_date=date(2026, 9, 3))["history"]) == 2


def test_daily_summary_includes_effective_goal_progress(
    repository: NutritionRepository, meal: object
) -> None:
    repository.create_entry(meal)  # type: ignore[arg-type]
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_000,
            targets=NutritionValues(protein_g=100),
            reason="Test goal",
        )
    )

    summary = repository.summarize(SummarizeInput(window=today(), grouping="day"))
    group = summary["groups"][0]
    assert group["group"] == "2026-08-27"
    assert group["goal"]["energy_budget"]["ordinary_target_kcal"] == 2_000
    assert group["goal_progress"]["calories_kcal"] == {
        "consumed": 484,
        "intake_complete": True,
        "ordinary_target": 2_000,
        "remaining_to_ordinary_target": 1_516,
        "planned_baseline": 2_000,
        "remaining_to_planned_baseline": 1_516,
        "available_ceiling": 2_000,
        "remaining_to_available_ceiling": 1_516,
    }


def test_single_day_whole_range_summary_includes_effective_goal(
    repository: NutritionRepository, meal: object
) -> None:
    repository.create_entry(meal)  # type: ignore[arg-type]
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_000,
            targets=NutritionValues(fat_g=0),
            reason="Test goal",
        )
    )

    summary = repository.summarize(SummarizeInput(window=today()))
    group = summary["groups"][0]
    assert group["group"] == "whole_range"
    assert group["goal"]["energy_budget"]["ordinary_target_kcal"] == 2_000
    assert group["goal_progress"]["calories_kcal"]["remaining_to_ordinary_target"] == 1_516
    assert group["goal_progress"]["fat_g"]["fraction"] is None


def test_training_crud_and_dynamic_calorie_budget(
    repository: NutritionRepository, meal: object
) -> None:
    repository.create_entry(meal)  # type: ignore[arg-type]
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_200,
            deficit_kcal=400,
            targets=NutritionValues(protein_g=120),
            reason="Cutting goal",
        )
    )
    request = LogTrainingInput(
        occurred_at=datetime.fromisoformat("2026-08-27T18:00:00+02:00"),
        activity="Cycling",
        duration_minutes=60,
        reported_burn_kcal=850,
        confidence="high",
        measurement_method="power_meter",
        source=TrainingSource(type="user_provided", detail="Cycling computer"),
    )
    created = repository.create_training(request)
    retry = repository.create_training(request)
    assert retry["training_id"] == created["training_id"]
    assert retry["deduplicated"] is True

    listed = repository.list_trainings(ListTrainingsInput(window=today()))
    assert listed["trainings"][0]["activity"] == "Cycling"
    assert [
        item["scheduled_kcal"]
        for item in repository.energy_balance(date(2026, 8, 27))["recovery_schedule"]
    ] == [400, 240, 160]
    updated = repository.update_training(
        created["training_id"],
        1,
        "Corrected from cycling computer",
        TrainingChanges(reported_burn_kcal=900),
    )
    assert updated["revision"] == 2
    assert [
        item["scheduled_kcal"]
        for item in repository.energy_balance(date(2026, 8, 27))["recovery_schedule"]
    ] == [400, 240, 160]
    assert (
        repository.training_revision_history(created["training_id"])[0]["snapshot"][
            "reported_burn_kcal"
        ]
        == 850
    )

    summary = repository.summarize(SummarizeInput(window=today()))
    group = summary["groups"][0]
    assert group["training_count"] == 1
    assert group["reported_training_burn_kcal"] == 900
    assert group["credited_training_burn_kcal"] == 900
    assert group["goal"]["energy_budget"]["ordinary_target_kcal"] == 1_800
    assert group["goal"]["energy_budget"]["available_ceiling_kcal"] == 2_700
    assert group["goal"]["energy_budget"]["unused_exercise_credit_kcal"] == 900
    assert group["goal_progress"]["calories_kcal"]["remaining_to_available_ceiling"] == 2_216

    repository.delete_training(created["training_id"], 2, "Training was duplicated")
    assert repository.training_revision_history(created["training_id"])[-1]["operation"] == "delete"
    assert repository.list_trainings(ListTrainingsInput(window=today()))["trainings"] == []
    assert repository.energy_balance(date(2026, 8, 27))["recovery_schedule"] == []
    assert (
        repository.get_goals(on_date=date(2026, 8, 27))["current"]["energy_budget"][
            "ordinary_target_kcal"
        ]
        == 1_800
    )


def test_confidence_adjustment_and_recovery_weights(repository: NutritionRepository) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Recovery policy fixture",
        )
    )
    repository.create_training(
        LogTrainingInput(
            occurred_at=datetime.fromisoformat("2026-08-27T08:00:00+02:00"),
            activity="Hike",
            duration_minutes=300,
            reported_burn_kcal=1_200,
            confidence="medium",
            measurement_method="heart_rate_gps_model",
            source=TrainingSource(type="wearable", detail="GPS watch"),
        )
    )

    balance = repository.energy_balance(date(2026, 8, 27))
    assert balance["policy_id"] == "energy-credit/v2"
    assert balance["reported_training_burn_kcal"] == 1_200
    assert balance["credited_training_burn_kcal"] == 960
    assert balance["unused_exercise_credit_kcal"] == 960
    assert [item["scheduled_kcal"] for item in balance["recovery_schedule"]] == [
        480,
        288,
        192,
    ]
    assert balance["exercise_credit_expired_at_creation_kcal"] == 0
    assert repository.energy_balance(date(2026, 8, 28))["incoming_recovery_kcal"] == 480


def test_large_credit_caps_pool_before_tapering(repository: NutritionRepository) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Large recovery fixture",
        )
    )
    repository.create_training(
        LogTrainingInput(
            occurred_at=datetime.fromisoformat("2026-08-27T08:00:00+02:00"),
            activity="Long power-meter ride",
            duration_minutes=300,
            reported_burn_kcal=2_800,
            confidence="high",
            measurement_method="power_meter",
            source=TrainingSource(type="wearable", detail="Power meter"),
        )
    )

    balance = repository.energy_balance(date(2026, 8, 27))
    assert balance["unused_exercise_credit_kcal"] == 2_800
    assert balance["recovery_pool_cap_kcal"] == 1_000
    assert balance["recovery_pool_kcal"] == 1_000
    assert balance["exercise_credit_excluded_from_recovery_kcal"] == 1_800
    assert [item["scheduled_kcal"] for item in balance["recovery_schedule"]] == [
        500,
        300,
        200,
    ]
    assert balance["recovery_pool_expired_at_creation_kcal"] == 0
    assert balance["exercise_credit_expired_at_creation_kcal"] == 1_800


def test_recovery_pool_uses_next_days_effective_deficit(
    repository: NutritionRepository,
) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Initial deficit",
        )
    )
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 28),
            base_burn_kcal=2_500,
            deficit_kcal=300,
            reason="Smaller deficit from tomorrow",
        )
    )
    repository.create_training(
        LogTrainingInput(
            occurred_at=datetime.fromisoformat("2026-08-27T08:00:00+02:00"),
            activity="Long power-meter ride",
            duration_minutes=300,
            reported_burn_kcal=2_000,
            confidence="high",
            measurement_method="power_meter",
            source=TrainingSource(type="wearable", detail="Power meter"),
        )
    )

    balance = repository.energy_balance(date(2026, 8, 27))
    assert balance["recovery_pool_cap_kcal"] == 600
    assert [item["scheduled_kcal"] for item in balance["recovery_schedule"]] == [
        300,
        180,
        120,
    ]


def test_all_confidence_multipliers_are_explicit(repository: NutritionRepository) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Confidence fixture",
        )
    )
    for hour, confidence in enumerate(Confidence, start=8):
        repository.create_training(
            LogTrainingInput(
                occurred_at=datetime.fromisoformat(f"2026-08-27T{hour:02d}:00:00+02:00"),
                activity=f"{confidence.value} confidence activity",
                duration_minutes=30,
                reported_burn_kcal=100,
                confidence=confidence,
                measurement_method="other",
                source=TrainingSource(type="other", detail="Test evidence"),
            )
        )

    balance = repository.energy_balance(date(2026, 8, 27))
    assert balance["reported_training_burn_kcal"] == 300
    assert balance["credited_training_burn_kcal"] == 240


def test_every_positive_unused_credit_is_distributed(repository: NutritionRepository) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Small credit fixture",
        )
    )
    repository.create_training(
        LogTrainingInput(
            occurred_at=datetime.fromisoformat("2026-08-27T08:00:00+02:00"),
            activity="Short ride",
            duration_minutes=10,
            reported_burn_kcal=1,
            confidence="high",
            measurement_method="power_meter",
            source=TrainingSource(type="wearable", detail="Power meter"),
        )
    )

    schedule = repository.energy_balance(date(2026, 8, 27))["recovery_schedule"]
    assert [item["scheduled_kcal"] for item in schedule] == [0.5, 0.3, 0.2]


def test_incoming_recovery_precedes_exercise_and_collisions_share_cap(
    repository: NutritionRepository,
) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Consecutive training fixture",
        )
    )
    for on_date in (date(2026, 8, 27), date(2026, 8, 28)):
        repository.create_training(
            LogTrainingInput(
                occurred_at=datetime.fromisoformat(f"{on_date.isoformat()}T08:00:00+02:00"),
                activity="Power-meter ride",
                duration_minutes=60,
                reported_burn_kcal=1_000,
                confidence="high",
                measurement_method="power_meter",
                source=TrainingSource(type="wearable", detail="Power meter"),
            )
        )
    repository.create_entry(calorie_entry(date(2026, 8, 28), 2_400))

    second_day = repository.energy_balance(date(2026, 8, 28))
    assert second_day["incoming_recovery_kcal"] == 500
    assert second_day["incoming_recovery_used_kcal"] == 400
    assert second_day["exercise_credit_used_kcal"] == 0
    assert second_day["unused_exercise_credit_kcal"] == 1_000

    first_day = repository.energy_balance(date(2026, 8, 27))
    day_three = first_day["recovery_schedule"][1]
    assert day_three["candidate_kcal"] == 300
    assert day_three["scheduled_kcal"] == 187.5
    assert repository.energy_balance(date(2026, 8, 29))["incoming_recovery_kcal"] == 500


def test_large_day_tapers_after_prior_training_collisions(
    repository: NutritionRepository,
) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Production-shaped recovery fixture",
        )
    )
    fixtures = [
        (date(2026, 8, 28), 425, "high", 2_140.45),
        (date(2026, 8, 29), 937, "high", 3_015.7),
        (date(2026, 8, 30), 3_917, "medium", 2_455.94),
    ]
    for on_date, burn, confidence, intake in fixtures:
        repository.create_training(
            LogTrainingInput(
                occurred_at=datetime.fromisoformat(f"{on_date.isoformat()}T08:00:00+02:00"),
                activity="Production-shaped training",
                duration_minutes=300,
                reported_burn_kcal=burn,
                confidence=confidence,
                measurement_method=(
                    "power_meter" if confidence == "high" else "heart_rate_gps_model"
                ),
                source=TrainingSource(type="wearable", detail="Test fixture"),
            )
        )
        repository.create_entry(calorie_entry(on_date, intake))

    large_day = repository.energy_balance(date(2026, 8, 30))
    assert large_day["unused_exercise_credit_kcal"] == 2_794.813
    assert large_day["recovery_pool_kcal"] == 1_000
    assert [item["candidate_kcal"] for item in large_day["recovery_schedule"]] == [
        500,
        300,
        200,
    ]
    assert [item["scheduled_kcal"] for item in large_day["recovery_schedule"]] == [
        434.041,
        300,
        200,
    ]
    assert [
        repository.energy_balance(on_date)["incoming_recovery_kcal"]
        for on_date in (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))
    ] == [500, 312.714, 200]


def test_incomplete_calorie_data_does_not_create_recovery_credit(
    repository: NutritionRepository,
) -> None:
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            base_burn_kcal=2_500,
            deficit_kcal=500,
            reason="Incomplete intake fixture",
        )
    )
    repository.create_entry(
        LogEntryInput.model_validate(
            {
                "occurred_at": "2026-08-27T12:00:00+02:00",
                "kind": "lunch",
                "title": "Calories unknown",
                "components": [
                    {
                        "name": "Unknown-calorie component",
                        "source": {"type": "estimated"},
                        "nutrition": {"protein_g": 10},
                    }
                ],
            }
        )
    )
    repository.create_training(
        LogTrainingInput(
            occurred_at=datetime.fromisoformat("2026-08-27T08:00:00+02:00"),
            activity="Ride",
            duration_minutes=60,
            reported_burn_kcal=800,
            confidence="high",
            measurement_method="power_meter",
            source=TrainingSource(type="wearable", detail="Power meter"),
        )
    )

    balance = repository.energy_balance(date(2026, 8, 27))
    assert balance["status"] == "incomplete_intake"
    assert balance["intake_complete"] is False
    assert balance["unused_exercise_credit_kcal"] is None
    assert balance["recovery_schedule"] == []
