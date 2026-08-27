from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from mcp_nutrition_db.models import (
    ComponentInput,
    GoalInput,
    IntervalWindow,
    LogEntryInput,
    NutritionValues,
    RelativeDayWindow,
    resolve_window,
)


def test_relative_today_resolves_without_agent_timestamp_math() -> None:
    window = RelativeDayWindow(type="relative_day", day="today", timezone="Europe/Zurich")
    resolved = resolve_window(window, now=datetime(2026, 8, 27, 12, tzinfo=UTC))

    assert resolved.start.isoformat() == "2026-08-26T22:00:00+00:00"
    assert resolved.end.isoformat() == "2026-08-27T22:00:00+00:00"


def test_relative_day_handles_dst_boundary() -> None:
    window = RelativeDayWindow(type="relative_day", day="today", timezone="Europe/Zurich")
    resolved = resolve_window(window, now=datetime(2026, 3, 29, 12, tzinfo=UTC))

    assert (resolved.end - resolved.start).total_seconds() == pytest.approx(23 * 60 * 60)


def test_explicit_window_requires_offset_and_is_bounded() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        IntervalWindow(
            type="interval",
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 2),
        )


def test_component_requires_source_and_some_nutrition() -> None:
    with pytest.raises(ValidationError, match="source"):
        ComponentInput.model_validate({"name": "Soup", "nutrition": {"calories_kcal": 100}})
    with pytest.raises(ValidationError, match="at least one nutrition"):
        NutritionValues()


def test_entry_rejects_naive_occurrence(meal_payload: dict[str, object]) -> None:
    meal_payload["occurred_at"] = "2026-08-27T12:30:00"
    with pytest.raises(ValidationError, match="UTC offset"):
        LogEntryInput.model_validate(meal_payload)


def test_goal_calories_are_derived() -> None:
    with pytest.raises(ValueError, match="derived"):
        GoalInput(
            effective_from=date(2026, 8, 27),
            base_burn_kcal=2200,
            targets={"calories_kcal": 1800},  # type: ignore[arg-type]
            reason="Invalid duplicate calorie target",
        )

    with pytest.raises(ValueError, match="less than"):
        GoalInput(
            effective_from=date(2026, 8, 27),
            base_burn_kcal=400,
            deficit_kcal=400,
            reason="Invalid deficit",
        )
