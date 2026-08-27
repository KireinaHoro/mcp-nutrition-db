from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mcp_nutrition_db.models import LogEntryInput
from mcp_nutrition_db.repository import NutritionRepository


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 8, 27, 12, 0, tzinfo=UTC))


@pytest.fixture
def repository(tmp_path: Path, clock: Clock) -> NutritionRepository:
    return NutritionRepository(tmp_path / "nutrition.sqlite3", clock=clock)


@pytest.fixture
def meal_payload() -> dict[str, Any]:
    return {
        "occurred_at": "2026-08-27T12:30:00+02:00",
        "timezone": "Europe/Zurich",
        "kind": "lunch",
        "title": "Rice bowl with salmon",
        "notes": "Estimated from a meal photo",
        "components": [
            {
                "name": "Cooked rice",
                "quantity": 180,
                "unit": "g",
                "portion_notes": "Amount confirmed by user",
                "source": {"type": "estimated", "detail": "Meal photo"},
                "nutrition": {
                    "calories_kcal": 234,
                    "protein_g": 4.3,
                    "carbohydrate_g": 51.5,
                    "fat_g": 0.5,
                },
            },
            {
                "name": "Grilled salmon",
                "quantity": 120,
                "unit": "g",
                "source": {"type": "database", "detail": "Generic cooked salmon"},
                "nutrition": {
                    "calories_kcal": 250,
                    "protein_g": 26,
                    "fat_g": 15,
                },
            },
        ],
        "estimation": {
            "confidence": "medium",
            "assumptions": ["Cooking oil was not visible"],
            "source": "meal_photo_and_user_clarification",
        },
    }


@pytest.fixture
def meal(meal_payload: dict[str, Any]) -> LogEntryInput:
    return LogEntryInput.model_validate(meal_payload)
