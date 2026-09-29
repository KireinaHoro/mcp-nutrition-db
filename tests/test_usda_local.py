from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
import zipfile

import pytest
from pydantic import ValidationError

from mcp_nutrition_db.inventory import InventoryError
from mcp_nutrition_db.inventory_models import FoodDefinition
from mcp_nutrition_db.models import LogEntryInput, QuantityAmount
from mcp_nutrition_db.usda import USDAClient
from mcp_nutrition_db.usda_dataset import build_database


def raw(fdc_id=123, description="Egg, whole, raw", calories=143):
    return {
        "fdcId": fdc_id,
        "description": description,
        "dataType": "Foundation",
        "foodNutrients": [{"nutrient": {"id": 1008, "unitName": "kcal"}, "amount": calories}],
    }


def definition(snapshot):
    return FoodDefinition.model_validate(
        {
            "name": snapshot["description"],
            "short_name": "eggs raw",
            "preparation": "raw",
            "weight_basis": "edible",
            "basis": snapshot["basis"],
            "nutrition": snapshot["nutrition"],
            "usda_fdc_id": snapshot["fdc_id"],
            "source": {
                "type": "database",
                "detail": "USDA local reference",
                "external_reference": {
                    "provider": "usda_fdc",
                    "record_id": snapshot["fdc_id"],
                    "source_snapshot_id": snapshot["source_snapshot_id"],
                },
            },
        }
    )


def test_offline_search_filtering_pagination_and_safe_queries(
    repository, usda_database, monkeypatch
):
    path = usda_database([raw(), raw(124, "Eggs, cooked"), raw(125, "Café beverage")])
    before = hashlib.sha256(path.read_bytes()).digest()

    def no_network(*args, **kwargs):
        raise AssertionError("USDA must never open a network socket")

    monkeypatch.setattr(socket, "socket", no_network)
    client = USDAClient(repository.database)
    first = client.search("eggs", limit=1)
    second = client.search("eggs", limit=1, page=2)
    assert first["total_results"] == 2 and first["total_pages"] == 2
    assert first["foods"][0]["fdcId"] != second["foods"][0]["fdcId"]
    assert client.search("cafe")["foods"][0]["fdcId"] == 125
    assert client.search("123")["foods"][0]["fdcId"] == 123
    assert client.search("9" * 200)["foods"] == []
    assert client.search('"; DROP TABLE foods; --')["foods"] == []
    assert client.search("!!!")["foods"] == []
    missing = client.search("eggs", data_types=["Branded"])
    assert missing["outcome"] == "unavailable"
    assert missing["unavailable_data_types"] == ["Branded"]
    assert client.get_food(123)["provider"] == "local_database"
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_dataset_upgrade_requires_explicit_inventory_revision_and_preserves_meals(
    repository, usda_database, meal_payload, monkeypatch
):
    old_path = usda_database([raw(), raw(124, "Old food")], release="2025")
    client = USDAClient(repository.database)
    lookup = client.search("egg")
    first = client.get_food(123)
    food = repository.inventory.create_food(definition(first))
    meal_payload["components"] = [
        {
            "food_id": food["food_id"],
            "food_revision": 1,
            "amount": {"type": "quantity", "quantity": 100, "unit": "g"},
        }
    ]
    meal = repository.create_entry(LogEntryInput.model_validate(meal_payload))
    meal = repository.get_entry(meal["entry_id"])
    usda_database([raw(calories=150)], release="2026")
    new_lookup = client.search("egg")
    second = client.get_food(123)
    assert new_lookup["lookup_id"] != lookup["lookup_id"]
    assert new_lookup["dataset"]["dataset_id"] != lookup["dataset"]["dataset_id"]
    assert first["source_snapshot_id"] != second["source_snapshot_id"]
    assert client.get_food(123)["source_snapshot_id"] == second["source_snapshot_id"]
    assert repository.inventory.get_food(food["food_id"]) == food
    assert repository.get_entry(meal["entry_id"]) == meal
    # The older release can be removed from disk without losing copied evidence.
    old_path.unlink()
    updated = repository.inventory.update_food(
        food["food_id"],
        1,
        "Adopt reviewed release",
        {
            "nutrition": second["nutrition"],
            "source": definition(second).source.model_dump(mode="json"),
        },
    )
    assert updated["revision"] == 2
    amount = QuantityAmount(type="quantity", quantity=100, unit="g")
    assert (
        repository.inventory.resolve_food(food["food_id"], 1, amount)["nutrition"]["calories_kcal"]
        == 143
    )
    assert (
        repository.inventory.resolve_food(food["food_id"], 2, amount)["nutrition"]["calories_kcal"]
        == 150
    )
    assert repository.get_entry(meal["entry_id"]) == meal
    # An ID removed from the installed release is not fetched from the origin.
    with pytest.raises(InventoryError, match="dataset_food_not_found"):
        client.get_food(124)
    usda_database([raw(999, "Replacement food")], release="2027")
    with pytest.raises(InventoryError, match="dataset_food_not_found"):
        client.get_food(123)
    assert (
        repository.inventory.get_food(food["food_id"], 1)["source_evidence"]["0"]["dataset"][
            "release"
        ]
        == "2025"
    )
    assert repository.get_entry(meal["entry_id"]) == meal
    monkeypatch.delenv("MCP_NUTRITION_USDA_DATABASE")
    assert client.search("egg")["outcome"] == "unavailable"
    assert (
        repository.inventory.resolve_food(food["food_id"], 2, amount)["nutrition"]["calories_kcal"]
        == 150
    )


def test_legacy_api_cache_is_not_used_for_local_lookups(repository, usda_database):
    with repository.database.connection() as connection:
        connection.execute(
            "INSERT INTO usda_snapshots VALUES (?, ?, ?, ?)",
            (
                "old-api-snapshot",
                123,
                json.dumps({"nutrition": {"calories_kcal": 999}}),
                "2099-01-01T00:00:00Z",
            ),
        )
    usda_database([raw()])
    snapshot = USDAClient(repository.database).get_food(123)
    assert snapshot["nutrition"]["calories_kcal"] == 143
    assert snapshot["dataset"]["release"] == "test-release"


@pytest.mark.parametrize("state", ["missing", "corrupt", "unsupported"])
def test_missing_or_bad_database_returns_failure_receipt(repository, tmp_path, monkeypatch, state):
    path = tmp_path / "bad.sqlite3"
    if state == "corrupt":
        path.write_bytes(b"not a database")
    elif state == "unsupported":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE metadata (key TEXT, value TEXT)")
            connection.execute(
                "INSERT INTO metadata VALUES ('dataset', ?)", (json.dumps({"format_version": 999}),)
            )
    monkeypatch.setenv("MCP_NUTRITION_USDA_DATABASE", str(path))
    result = USDAClient(repository.database).search("egg")
    assert result["outcome"] == "unavailable"
    assert result["error"]["code"] == "provider_unavailable"
    assert path.exists() == (state != "missing")
    with repository.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM usda_lookups").fetchone()[0] == 1


def test_bulk_null_slots_missing_nutrition_and_negative_values(repository, usda_database):
    empty = raw(124, "Human milk, nutrients withdrawn")
    empty["foodNutrients"] = []
    negative = raw(125, "Food with negative carbohydrate by difference")
    negative["foodNutrients"].append(
        {
            "nutrient": {"id": 1005, "unitName": "g"},
            "amount": -0.475,
        }
    )
    usda_database([raw(), None, empty, negative])
    client = USDAClient(repository.database)
    assert client.search("egg")["dataset"]["null_slots_skipped"] == {"Foundation": 1}
    empty_snapshot = client.get_food(124)
    assert not empty_snapshot["nutrition_available"]
    assert all(v is None for v in empty_snapshot["nutrition"].values())
    with pytest.raises(ValidationError):
        definition(empty_snapshot)
    snapshot = client.get_food(125)
    assert snapshot["nutrition"]["carbohydrate_g"] is None
    assert snapshot["normalization_notes"][0]["value"] == -0.475
    assert snapshot["raw"]["foodNutrients"][1]["amount"] == -0.475


def test_bulk_checksum_and_duplicate_id_fail_without_partial_database(tmp_path):
    archive = tmp_path / "input.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("foods.json", json.dumps({"Foods": [raw(), raw()]}))
    source = {
        "path": str(archive),
        "data_type": "Foundation",
        "release": "test",
        "url": "https://example.invalid/input.zip",
        "sha256": "incorrect",
    }
    output = tmp_path / "output.sqlite3"
    with pytest.raises(ValueError, match="checksum"):
        build_database(output, [source])
    assert not output.exists()
    source["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(sqlite3.IntegrityError):
        build_database(output, [source])
    assert not output.exists()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("foods.json", json.dumps({"Foods": [raw()]}))
    source["sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    first = build_database(output, [source])
    other = tmp_path / "other.sqlite3"
    second = build_database(other, [copy.deepcopy(source)])
    assert first == second
    assert output.read_bytes() == other.read_bytes()
