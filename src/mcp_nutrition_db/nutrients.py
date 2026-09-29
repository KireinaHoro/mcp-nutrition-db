"""Nutrient precision and aggregation with explicit completeness."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from .models import NutritionValues

NUTRIENTS: dict[str, tuple[str, int]] = {
    "calories_kcal": ("calories_mkcal", 1_000),
    "protein_g": ("protein_mg", 1_000),
    "carbohydrate_g": ("carbohydrate_mg", 1_000),
    "fat_g": ("fat_mg", 1_000),
    "fiber_g": ("fiber_mg", 1_000),
    "sugar_g": ("sugar_mg", 1_000),
    "sodium_mg": ("sodium_mg", 1),
}


def scale(value: float | None, factor: int) -> int | None:
    if value is None:
        return None
    return int((Decimal(str(value)) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def unscale(value: int | None, factor: int) -> float | None:
    if value is None:
        return None
    return float(Decimal(value) / factor)


def nutrient_db_values(nutrition: NutritionValues) -> dict[str, int | None]:
    return {
        column: scale(getattr(nutrition, public_name), factor)
        for public_name, (column, factor) in NUTRIENTS.items()
    }


def nutrient_public_values(
    row: sqlite3.Row | Mapping[str, Any],
) -> dict[str, float | None]:
    return {
        public_name: unscale(row[column], factor)
        for public_name, (column, factor) in NUTRIENTS.items()
    }


def aggregate_nutrition(
    records: Sequence[dict[str, Any]], *, level: Literal["components", "entries"]
) -> tuple[dict[str, Any], dict[str, Any]]:
    field = "nutrition" if level == "components" else "totals"
    totals = {}
    completeness = {}
    for nutrient, (_, factor) in NUTRIENTS.items():
        known = [
            record[field][nutrient] for record in records if record[field][nutrient] is not None
        ]
        totals[nutrient] = (
            None
            if not known
            else unscale(sum(scale(value, factor) or 0 for value in known), factor)
        )
        complete = len(known) == len(records) and (
            level == "components"
            or all(record["completeness"][nutrient]["complete"] for record in records)
        )
        completeness[nutrient] = {
            f"known_{level}": len(known),
            f"total_{level}": len(records),
            "complete": complete,
        }
    return totals, completeness
