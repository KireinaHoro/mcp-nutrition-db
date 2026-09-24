"""Bounded USDA FoodData Central retrieval with immutable provenance receipts."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .inventory import InventoryError, _json
from .models import NutritionValues
from .repository import _new_id, _timestamp

if TYPE_CHECKING:
    from .repository import NutritionRepository

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


def api_key() -> str | None:
    key_file = os.environ.get("MCP_NUTRITION_USDA_API_KEY_FILE")
    if key_file:
        try:
            return Path(key_file).read_text().strip() or None
        except OSError:
            return None
    return os.environ.get("MCP_NUTRITION_USDA_API_KEY") or None


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
    for name, candidates in NUTRIENT_IDS.items():
        values[name] = None
        for nutrient_id, unit in candidates:
            if nutrient_id in nutrients:
                amount, actual_unit = nutrients[nutrient_id]
                if actual_unit != unit:
                    raise ValueError("unexpected USDA nutrient unit")
                values[name] = amount
                selected[name] = nutrient_id
                break
    nutrition = NutritionValues.model_validate(values)
    return {
        "fdc_id": int(raw["fdcId"]),
        "description": raw["description"],
        "data_type": raw["dataType"],
        "basis": {"quantity": 100, "unit": "g"},
        "nutrition": nutrition.model_dump(),
        "selected_nutrient_ids": selected,
        "portions": raw.get("foodPortions", []),
        "brand": raw.get("brandOwner"),
        "source_url": f"https://fdc.nal.usda.gov/food-details/{int(raw['fdcId'])}/nutrients",
        "raw": raw,
    }


class USDAClient:
    def __init__(self, repository: NutritionRepository) -> None:
        self.repo = repository

    @staticmethod
    def _request(path: str, params: dict[str, Any]) -> dict[str, Any]:
        key = api_key()
        if not key:
            raise InventoryError("provider_unavailable", reason="USDA API key is not configured")
        url = (
            "https://api.nal.usda.gov/fdc/v1/"
            + path
            + "?"
            + urlencode({**params, "api_key": key}, doseq=True)
        )
        request = Request(url, headers={"Accept": "application/json"})
        # One retry only for transient upstream 5xx responses; never echo URL/key.
        for attempt in range(2):
            try:
                with urlopen(request, timeout=10) as response:
                    data = response.read(5_000_001)
                if len(data) > 5_000_000:
                    raise InventoryError("provider_unavailable", reason="USDA response too large")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("invalid response")
                return result
            except HTTPError as error:
                if 500 <= error.code < 600 and attempt == 0:
                    continue
                raise InventoryError(
                    "provider_rate_limited" if error.code == 429 else "provider_unavailable",
                    status=error.code,
                ) from None
            except (URLError, TimeoutError, OSError, ValueError):
                raise InventoryError(
                    "provider_unavailable", reason="USDA retrieval failed"
                ) from None
        raise InventoryError("provider_unavailable")

    def search(
        self, query: str, data_types: list[str] | None = None, page: int = 1, limit: int = 20
    ) -> dict[str, Any]:
        if data_types and any(t not in DATA_TYPES for t in data_types):
            raise ValueError("unsupported USDA data type")
        params: dict[str, Any] = {"query": query, "pageNumber": page, "pageSize": limit}
        if data_types:
            params["dataType"] = sorted(set(data_types))
        request_json = _json(params)
        cutoff = _timestamp(self.repo.clock() - timedelta(days=1))
        with self.repo._connect() as connection:
            rows = connection.execute(
                "SELECT result_json FROM usda_lookups WHERE request_json=? AND "
                "created_at>=? ORDER BY created_at DESC",
                (request_json, cutoff),
            ).fetchall()
        for row in rows:
            cached: dict[str, Any] = json.loads(row["result_json"])
            if cached["outcome"] == "success":
                return {**cached, "cached": True}
        lookup_id = _new_id()
        now = _timestamp(self.repo.clock())
        result: dict[str, Any] = {
            "lookup_id": lookup_id,
            "query": query,
            "retrieved_at": now,
            "cached": False,
        }
        try:
            raw = self._request("foods/search", params)
            result.update(
                {
                    "outcome": "success",
                    "page": page,
                    "total_pages": raw.get("totalPages", 0),
                    "foods": [
                        {
                            k: f.get(k)
                            for k in (
                                "fdcId",
                                "description",
                                "dataType",
                                "brandOwner",
                                "brandName",
                                "ingredients",
                            )
                        }
                        for f in raw.get("foods", [])
                    ],
                }
            )
        except InventoryError as error:
            result.update(
                {
                    "outcome": "unavailable",
                    "error": {"code": error.code, **error.details},
                    "foods": [],
                }
            )
        with self.repo._connect() as connection:
            connection.execute(
                "INSERT INTO usda_lookups VALUES (?, ?, ?, ?)",
                (lookup_id, request_json, _json(result), now),
            )
        return result

    def get_food(self, fdc_id: int) -> dict[str, Any]:
        cutoff = _timestamp(self.repo.clock() - timedelta(days=1))
        with self.repo._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM usda_snapshots WHERE fdc_id=? AND "
                "created_at>=? ORDER BY created_at DESC LIMIT 1",
                (fdc_id, cutoff),
            ).fetchone()
        if row:
            cached: dict[str, Any] = json.loads(row["snapshot_json"])
            return {**cached, "cached": True}
        raw = self._request(f"food/{fdc_id}", {"format": "full"})
        try:
            snapshot = normalize_food(raw)
            if snapshot["fdc_id"] != fdc_id:
                raise ValueError("mismatched FDC ID")
        except (ValueError, KeyError, TypeError):
            raise InventoryError(
                "provider_unavailable", reason="unsupported or invalid USDA food response"
            ) from None
        snapshot_id = _new_id()
        now = _timestamp(self.repo.clock())
        snapshot.update({"source_snapshot_id": snapshot_id, "retrieved_at": now})
        with self.repo._connect() as connection:
            connection.execute(
                "INSERT INTO usda_snapshots VALUES (?, ?, ?, ?)",
                (snapshot_id, fdc_id, _json(snapshot), now),
            )
        return {**snapshot, "cached": False}
