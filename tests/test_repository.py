from __future__ import annotations

from datetime import date

import pytest

from mcp_nutrition_db.models import (
    EntryChanges,
    GoalInput,
    ListEntriesInput,
    NutritionValues,
    RelativeDayWindow,
    SummarizeInput,
)
from mcp_nutrition_db.repository import (
    NotFoundError,
    NutritionRepository,
    RevisionConflictError,
)


def today() -> RelativeDayWindow:
    return RelativeDayWindow(type="relative_day", day="today", timezone="Europe/Zurich")


def test_schema_migration_is_repeatable(repository: NutritionRepository) -> None:
    assert repository.schema_version() == 1
    repository.migrate()
    assert repository.schema_version() == 1


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
            targets=NutritionValues(calories_kcal=2_000, protein_g=120),
            reason="Initial goal",
        )
    )
    september = repository.set_goals(
        GoalInput(
            effective_from=date(2026, 9, 1),
            targets=NutritionValues(calories_kcal=2_200, protein_g=130),
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
            targets=NutritionValues(calories_kcal=2_100),
            reason="Corrected target",
        )
    )
    assert replacement["goal_id"] == september["goal_id"]
    assert replacement["targets"]["calories_kcal"] == 2_100
    assert len(repository.get_goals(on_date=date(2026, 9, 3))["history"]) == 2


def test_daily_summary_includes_effective_goal_progress(
    repository: NutritionRepository, meal: object
) -> None:
    repository.create_entry(meal)  # type: ignore[arg-type]
    repository.set_goals(
        GoalInput(
            effective_from=date(2026, 8, 1),
            targets=NutritionValues(calories_kcal=2_000, protein_g=100),
            reason="Test goal",
        )
    )

    summary = repository.summarize(SummarizeInput(window=today(), grouping="day"))
    group = summary["groups"][0]
    assert group["group"] == "2026-08-27"
    assert group["goal"]["targets"]["calories_kcal"] == 2_000
    assert group["goal_progress"]["calories_kcal"] == {
        "target": 2_000,
        "consumed": 484,
        "remaining": 1_516,
        "fraction": 0.242,
    }
