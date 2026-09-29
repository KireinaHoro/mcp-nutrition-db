"""Normalize official USDA bulk records without runtime database dependencies."""

from __future__ import annotations

from typing import Any

from .models import NutritionValues

DATA_TYPES = ("Foundation", "SR Legacy", "Survey (FNDDS)", "Branded")
# Priority is deliberate: use one energy measure, never sum alternatives.
NUTRIENT_IDS = {
    "calories_kcal": ((2048, "kcal"), (2047, "kcal"), (1008, "kcal")),
    "protein_g": ((1003, "g"),),
    "carbohydrate_g": ((1005, "g"),),
    "fat_g": ((1004, "g"),),
    "fiber_g": ((1079, "g"),),
    "sugar_g": ((2000, "g"), (1063, "g")),
    "sodium_mg": ((1093, "mg"),),
}


def normalize_food(raw: dict[str, Any]) -> dict[str, Any]:
    """FDC full foodNutrients are per 100 g; labelNutrients are NOT interchangeable."""
    if raw.get("dataType") not in DATA_TYPES:
        raise ValueError("unsupported USDA data type")
    nutrients = {}
    for value in raw.get("foodNutrients", []):
        nutrient = value.get("nutrient", {})
        if nutrient.get("id") is not None and value.get("amount") is not None:
            nutrients[int(nutrient["id"])] = (
                value["amount"],
                nutrient.get("unitName", "").casefold(),
            )
    values: dict[str, Any] = {}
    selected = {}
    normalization_notes = []
    for name, candidates in NUTRIENT_IDS.items():
        values[name] = None
        for nutrient_id, unit in candidates:
            if nutrient_id in nutrients:
                amount, actual_unit = nutrients[nutrient_id]
                if actual_unit != unit:
                    raise ValueError("unexpected USDA nutrient unit")
                if isinstance(amount, (int, float)) and amount < 0:
                    normalization_notes.append(
                        {
                            "field": name,
                            "nutrient_id": nutrient_id,
                            "issue": "negative_source_value_preserved_as_unknown",
                            "value": amount,
                        }
                    )
                    break
                values[name] = amount
                selected[name] = nutrient_id
                break
    nutrition = NutritionValues.model_validate(values).model_dump() if selected else values
    return {
        "fdc_id": int(raw["fdcId"]),
        "description": raw["description"],
        "data_type": raw["dataType"],
        "basis": {"quantity": 100, "unit": "g"},
        "nutrition": nutrition,
        "nutrition_available": bool(selected),
        "selected_nutrient_ids": selected,
        "normalization_notes": normalization_notes,
        "portions": raw.get("foodPortions", []),
        "brand": raw.get("brandOwner"),
        "source_url": f"https://fdc.nal.usda.gov/food-details/{int(raw['fdcId'])}/nutrients",
        "raw": raw,
    }
