from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from mcp_nutrition_db.inventory import InventoryError, normalize_name
from mcp_nutrition_db.inventory_models import FoodDefinition, FoodLink
from mcp_nutrition_db.models import (
    EntryChanges,
    FoodAmount,
    LogEntryInput,
    RelativeDayWindow,
    SummarizeInput,
)
from mcp_nutrition_db.repository import NutritionRepository
from mcp_nutrition_db.server import create_server
from mcp_nutrition_db.usda import USDAClient, normalize_food


def food_payload(**changes: Any) -> dict[str, Any]:
    return {
        "name": "Coop Surimi",
        "short_name": "coop surimi",
        "preparation": "as_sold",
        "weight_basis": "edible",
        "basis": {"quantity": 100, "unit": "g"},
        "nutrition": {"calories_kcal": 123, "protein_g": 8.31, "sodium_mg": 401},
        "source": {"type": "nutrition_label", "detail": "Exact product label supplied by user"},
        "servings": [
            {
                "serving_key": "pack",
                "label": "500 g pack",
                "kind": "fixed",
                "amount": {"quantity": 500, "unit": "g"},
                "certainty": "declared",
            },
            {
                "serving_key": "variable_pack",
                "label": "Weighed pack",
                "kind": "variable",
                "unit": "g",
            },
            {
                "serving_key": "piece",
                "label": "Typical piece",
                "kind": "fixed",
                "amount": {"quantity": 30, "unit": "g"},
                "certainty": "estimated",
                "assumptions": ["Individual pieces vary in weight"],
            },
        ],
        **changes,
    }


def make_food(repository: NutritionRepository, **changes: Any) -> dict[str, Any]:
    return repository.inventory.create_food(FoodDefinition.model_validate(food_payload(**changes)))


def amount(value: dict[str, Any]) -> FoodAmount:
    return TypeAdapter(FoodAmount).validate_python(value)


@pytest.mark.parametrize(
    "short_name", [" coop  surimi ", "COOP SURIMI", "\uff23\uff4f\uff4f\uff50 Surimi"]
)
def test_short_name_conflicts_are_permanent_and_transactional(repository, short_name):
    food = make_food(repository)
    inv = repository.inventory
    inv.update_food(
        food["food_id"], 1, "Archive", {"status": "archived", "short_name": "surimi coop"}
    )
    with pytest.raises(InventoryError) as caught:
        make_food(repository, short_name=short_name)
    assert caught.value.code == "food_identity_conflict"
    assert caught.value.details["conflicts"][0]["food_id"] == food["food_id"]
    assert len(inv.search_foods(status="all")["foods"]) == 1
    assert normalize_name(short_name) == "coop surimi"


def test_concurrent_creation_cannot_duplicate(repository):
    def attempt(_):
        try:
            return make_food(repository)["food_id"]
        except InventoryError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert results.count("food_identity_conflict") == 1
    assert len(repository.inventory.search_foods()["foods"]) == 1


def test_identity_update_reports_all_conflicts_and_retains_aliases(repository):
    a = make_food(repository, identifiers=[{"scheme": "gtin", "value": "12345678"}])
    b = make_food(repository, short_name="other surimi")
    with pytest.raises(InventoryError) as caught:
        make_food(
            repository,
            short_name=b["short_name"],
            identifiers=[{"scheme": "gtin", "value": "12345678"}],
        )
    assert {c["food_id"] for c in caught.value.details["conflicts"]} == {a["food_id"], b["food_id"]}
    with pytest.raises(InventoryError):
        repository.inventory.update_food(b["food_id"], 1, "Rename", {"short_name": a["short_name"]})
    changed = repository.inventory.update_food(
        a["food_id"], 1, "Alias", {"aliases": ["surimi sticks"]}
    )
    assert changed["revision"] == 2
    assert repository.inventory.search_foods("SURIMI sticks")["foods"][0]["food_id"] == a["food_id"]
    assert repository.inventory.get_food(a["food_id"], 1)["aliases"] == []


@pytest.mark.parametrize(
    ("value", "grams", "kcal"),
    [
        ({"type": "serving", "serving_key": "pack", "count": 0.5}, 250, 307.5),
        (
            {
                "type": "variable_serving",
                "serving_key": "variable_pack",
                "whole_amount": {"quantity": 720, "unit": "g"},
                "fraction": 0.5,
            },
            360,
            442.8,
        ),
        ({"type": "serving", "serving_key": "piece", "count": 0.5}, 15, 18.45),
        ({"type": "quantity", "quantity": 90, "unit": "g"}, 90, 110.7),
    ],
)
def test_portion_cases_preserve_unknown_and_round_once(repository, value, grams, kcal):
    food = make_food(repository)
    result = repository.inventory.resolve_food(food["food_id"], 1, amount(value))
    assert result["quantity"] == grams
    assert result["nutrition"]["calories_kcal"] == kcal
    assert result["nutrition"]["fiber_g"] is None
    if value.get("serving_key") == "piece":
        assert result["inventory"]["serving_assumptions"]
        assert result["nutrition"]["protein_g"] == 1.247
        assert result["nutrition"]["sodium_mg"] == 60


def test_invalid_portions_and_revisions(repository):
    food = make_food(repository)
    inv = repository.inventory
    for value in [
        {"type": "serving", "serving_key": "variable_pack", "count": 1},
        {"type": "quantity", "quantity": 90, "unit": "ml"},
        {"type": "serving", "serving_key": "missing", "count": 1},
    ]:
        with pytest.raises(InventoryError):
            inv.resolve_food(food["food_id"], 1, amount(value))
    with pytest.raises(ValidationError):
        amount({"type": "quantity", "quantity": float("nan"), "unit": "g"})
    with pytest.raises(InventoryError):
        inv.resolve_food(
            food["food_id"], 99, amount({"type": "quantity", "quantity": 1, "unit": "g"})
        )
    item = make_food(
        repository, short_name="bakery piece", basis={"quantity": 1, "unit": "item"}, servings=[]
    )
    assert (
        inv.resolve_food(
            item["food_id"], 1, amount({"type": "quantity", "quantity": 0.5, "unit": "item"})
        )["nutrition"]["calories_kcal"]
        == 61.5
    )


def test_mixed_meals_pinned_retries_and_retention(repository, meal_payload):
    food = make_food(repository)
    reference = {
        "food_id": food["food_id"],
        "food_revision": 1,
        "amount": {"type": "serving", "serving_key": "pack", "count": 0.5},
    }
    meal_payload["components"][0] = reference
    request = LogEntryInput.model_validate(meal_payload)
    entry = repository.create_entry(request)
    inv_component = entry["components"][0]
    repository.inventory.update_food(
        food["food_id"], 1, "Correct label", {"nutrition": {"calories_kcal": 140}}
    )
    assert repository.create_entry(request)["entry_id"] == entry["entry_id"]
    assert repository.get_entry(entry["entry_id"])["components"][0] == inv_component
    updated = repository.update_entry(
        entry["entry_id"],
        1,
        "Retain all",
        EntryChanges.model_validate(
            {
                "components": [
                    {"existing_component_id": c["component_id"]} for c in entry["components"]
                ]
            }
        ),
    )
    assert updated["components"] == entry["components"]
    repository.inventory.update_food(food["food_id"], 2, "Archive", {"status": "archived"})
    assert (
        repository.update_entry(entry["entry_id"], 2, "Notes", EntryChanges(notes="Updated"))[
            "components"
        ]
        == updated["components"]
    )
    with pytest.raises(InventoryError):
        repository.inventory.resolve_food(food["food_id"], 1, amount(reference["amount"]))
    with pytest.raises(ValueError):
        repository.update_entry(
            entry["entry_id"],
            3,
            "Bad retain",
            EntryChanges.model_validate(
                {"components": [{"existing_component_id": inv_component["component_id"]}] * 2}
            ),
        )
    assert repository.get_entry(entry["entry_id"])["revision"] == 3


def link_for(entry, food, component=0, **changes):
    return FoodLink.model_validate(
        {
            "entry_id": entry["entry_id"],
            "expected_entry_revision": entry["revision"],
            "component_id": entry["components"][component]["component_id"],
            "food_id": food["food_id"],
            "food_revision": food["revision"],
            "identity_evidence": "Reviewed original product evidence",
            **changes,
        }
    )


def test_links_preserve_entire_history_and_accounting(repository, meal, clock):
    entry = repository.create_entry(meal)
    food = make_food(repository)
    window = RelativeDayWindow(type="relative_day", day="today")
    before_summary = repository.summarize(SummarizeInput(window=window))
    plan = repository.inventory.preview_food_links(
        [
            link_for(entry, food, amount={"type": "quantity", "quantity": 180, "unit": "g"}),
            link_for(entry, food, component=1),
        ]
    )
    assert [p["comparison"] for p in plan["links"]] == ["different", "unverifiable"]
    result = repository.inventory.apply_food_links(plan["plan_id"], "Reviewed food identities")
    after = repository.get_entry(entry["entry_id"])
    assert after["revision"] == 2
    for before_c, after_c in zip(entry["components"], after["components"], strict=True):
        assert {k: v for k, v in after_c.items() if k != "inventory"} == {
            k: v for k, v in before_c.items() if k != "inventory"
        }
        assert after_c["inventory"]["nutrition_mode"] == "historical_snapshot"
    assert repository.summarize(SummarizeInput(window=window)) == before_summary
    clock.value += timedelta(days=2)
    assert repository.inventory.apply_food_links(plan["plan_id"], "Retry") == result
    with repository._connect() as connection:
        audit = connection.execute(
            "SELECT * FROM entry_revisions WHERE entry_id=?", (entry["entry_id"],)
        ).fetchone()
        assert json.loads(audit["snapshot_json"])["components"] == entry["components"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_stale_plan_all_or_nothing_and_expiry(repository, meal, clock):
    first = repository.create_entry(meal)
    second = repository.create_entry(meal.model_copy(update={"force_new": True}))
    food = make_food(repository)
    links = [link_for(first, food), link_for(second, food)]
    plan = repository.inventory.preview_food_links(links)
    repository.update_entry(
        second["entry_id"], 1, "Concurrent correction", EntryChanges(title="Corrected")
    )
    with pytest.raises(InventoryError, match="plan_conflict"):
        repository.inventory.apply_food_links(plan["plan_id"], "Scrub")
    assert repository.get_entry(first["entry_id"])["components"][0]["inventory"] is None
    fresh = repository.inventory.preview_food_links([links[0]])
    clock.value += timedelta(hours=25)
    with pytest.raises(InventoryError, match="plan_expired"):
        repository.inventory.apply_food_links(fresh["plan_id"], "Expired")


def test_history_pagination_and_no_automatic_name_matching(repository, meal):
    entry = repository.create_entry(meal)
    window = RelativeDayWindow(type="relative_day", day="today")
    first = repository.inventory.find_food_matches(window, limit=1)
    second = repository.inventory.find_food_matches(window, cursor=first["next_cursor"], limit=1)
    assert (
        first["matches"][0]["component"]["component_id"]
        != second["matches"][0]["component"]["component_id"]
    )
    assert second["next_cursor"] is None
    assert first["matches"][0]["entry_id"] == entry["entry_id"]
    with pytest.raises(ValueError):
        repository.inventory.find_food_matches(
            window, query="different", cursor=first["next_cursor"]
        )


def usda_raw(data_type="Foundation", fdc_id=123):
    return {
        "fdcId": fdc_id,
        "description": "Test USDA food",
        "dataType": data_type,
        "foodNutrients": [
            {"nutrient": {"id": 1008, "unitName": "kcal"}, "amount": 121},
            {"nutrient": {"id": 2048, "unitName": "kcal"}, "amount": 120},
            {"nutrient": {"id": 1003, "unitName": "g"}, "amount": 7},
            {"nutrient": {"id": 1093, "unitName": "mg"}, "amount": 10},
        ],
        "labelNutrients": {"calories": {"value": 999}},
    }


@pytest.mark.parametrize("data_type", ["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"])
def test_usda_nutrient_mapping(data_type):
    result = normalize_food(usda_raw(data_type))
    assert result["nutrition"]["calories_kcal"] == 120
    assert result["nutrition"]["fat_g"] is None
    assert result["basis"] == {"quantity": 100, "unit": "g"}
    raw = usda_raw(data_type)
    raw["foodNutrients"][1]["nutrient"]["unitName"] = "kJ"
    with pytest.raises(ValueError):
        normalize_food(raw)


def test_usda_receipts_direct_import_unique_id_and_snapshot(repository, usda_database):
    usda_database([usda_raw()])
    client = USDAClient(repository)
    search = client.search("test")
    assert client.search("test")["lookup_id"] == search["lookup_id"]
    snapshot = client.get_food(123)
    assert client.get_food(123)["source_snapshot_id"] == snapshot["source_snapshot_id"]
    payload = food_payload(
        usda_fdc_id=123,
        nutrition=snapshot["nutrition"],
        source={
            "type": "database",
            "detail": "USDA retrieved food",
            "external_reference": {
                "provider": "usda_fdc",
                "record_id": 123,
                "source_snapshot_id": snapshot["source_snapshot_id"],
            },
        },
    )
    food = repository.inventory.create_food(FoodDefinition.model_validate(payload))
    different = copy.deepcopy(payload)
    different["short_name"] = "other name"
    with pytest.raises(InventoryError, match="usda_fdc_id"):
        repository.inventory.create_food(FoodDefinition.model_validate(different))
    different["nutrition"]["calories_kcal"] = 999
    with pytest.raises(ValueError, match="conflict"):
        repository.inventory.create_food(FoodDefinition.model_validate(different))
    assert food["source_evidence"]["0"]["retrieved_at"] == snapshot["retrieved_at"]


def test_estimates_require_real_fallback_evidence(repository, monkeypatch):
    monkeypatch.delenv("MCP_NUTRITION_USDA_DATABASE", raising=False)
    payload = food_payload(
        source={"type": "estimated", "detail": "Recipe estimate", "method": "model_estimate"},
        estimation={"confidence": "low", "assumptions": ["Recipe is unknown"], "source": "model"},
    )
    with pytest.raises(ValidationError):
        FoodDefinition.model_validate(payload)
    payload["usda_lookup"] = {"outcome": "no_suitable_match", "fallback_reason": "Unknown recipe"}
    with pytest.raises(ValueError, match="receipts"):
        repository.inventory.create_food(FoodDefinition.model_validate(payload))

    receipt = USDAClient(repository).search("bakery")
    assert receipt["outcome"] == "unavailable"
    payload["usda_lookup"].update(outcome="unavailable", lookup_ids=[receipt["lookup_id"]])
    assert (
        repository.inventory.create_food(FoodDefinition.model_validate(payload))["source"]["type"]
        == "estimated"
    )


def test_historical_source_references_survive_entry_revision(repository, meal):
    entry = repository.create_entry(meal)
    payload = food_payload(
        source={
            "type": "estimated",
            "detail": "Original historical estimate",
            "method": "historical_import",
            "historical_reference": {
                "entry_id": entry["entry_id"],
                "entry_revision": 1,
                "component_id": entry["components"][0]["component_id"],
            },
        },
        estimation={
            "confidence": "medium",
            "assumptions": ["Preserved original estimate"],
            "source": "historical record",
        },
    )
    repository.update_entry(entry["entry_id"], 1, "Correct title", EntryChanges(title="New title"))
    food = repository.inventory.create_food(FoodDefinition.model_validate(payload))
    assert food["source_evidence"]["0"]["component_id"] == entry["components"][0]["component_id"]


def test_inventory_mcp_create_log_conflict_and_link(repository, meal):
    server = create_server(repository)
    app = server.streamable_http_app()

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8787",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client,
        ):
            request_id = 0

            async def call(name, arguments, expect_error=False):
                nonlocal request_id
                request_id += 1
                response = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                response.raise_for_status()
                result = response.json()["result"]
                assert bool(result.get("isError")) == expect_error
                return result if expect_error else result["structuredContent"]

            created = await call("nutrition_create_food", {"food": food_payload()})
            conflict = await call("nutrition_create_food", {"food": food_payload()}, True)
            assert "food_identity_conflict" in str(conflict)
            resolved = await call(
                "nutrition_resolve_food",
                {
                    "food_id": created["food_id"],
                    "food_revision": 1,
                    "amount": {"type": "serving", "serving_key": "pack", "count": 0.5},
                },
            )
            assert resolved["nutrition"]["calories_kcal"] == 307.5
            entry = repository.create_entry(meal)
            plan = await call(
                "nutrition_preview_food_links",
                {
                    "links": [link_for(entry, created).model_dump(mode="json")],
                },
            )
            applied = await call(
                "nutrition_apply_food_links",
                {
                    "plan_id": plan["plan_id"],
                    "reason": "Reviewed historical food",
                },
            )
            assert applied["linked_component_count"] == 1
            status = await call("nutrition_inventory_status", {})
            assert status["history"]["linked_components"] == 1
            payload = meal.model_dump(mode="json")
            payload["force_new"] = True
            payload["components"][0] = {
                "food_id": created["food_id"],
                "food_revision": 1,
                "amount": {"type": "quantity", "quantity": 100, "unit": "g"},
            }
            logged = await call("nutrition_log_entry", payload)
            assert logged["components"][0]["nutrition"]["calories_kcal"] == 123

    asyncio.run(exercise())


def test_direct_usda_cannot_be_hidden_in_mixed_source(repository, usda_database):
    usda_database([usda_raw()])
    snapshot = USDAClient(repository).get_food(123)
    source = {
        "type": "database",
        "detail": "Direct USDA",
        "external_reference": {
            "provider": "usda_fdc",
            "record_id": 123,
            "source_snapshot_id": snapshot["source_snapshot_id"],
        },
    }
    with pytest.raises(ValidationError, match="whole food"):
        FoodDefinition.model_validate(
            food_payload(
                nutrition={"calories_kcal": 120},
                source={
                    "type": "mixed",
                    "detail": "Mixed",
                    "nutrient_sources": {"calories_kcal": source},
                },
            )
        )


def test_two_proxy_estimates_reference_one_usda_identity(repository, usda_database):
    usda_database([usda_raw()])
    receipt = USDAClient(repository).search("bakery")
    snapshot = USDAClient(repository).get_food(123)
    canonical = make_food(
        repository,
        usda_fdc_id=123,
        nutrition=snapshot["nutrition"],
        source={
            "type": "database",
            "detail": "USDA",
            "external_reference": {
                "provider": "usda_fdc",
                "record_id": 123,
                "source_snapshot_id": snapshot["source_snapshot_id"],
            },
        },
    )
    for short_name in ["bakery a pastry", "bakery b pastry"]:
        food = make_food(
            repository,
            short_name=short_name,
            source={
                "type": "estimated",
                "detail": "Estimated using previously retrieved analogue",
                "inventory_reference": {"food_id": canonical["food_id"], "food_revision": 1},
            },
            estimation={
                "confidence": "low",
                "source": "USDA proxy",
                "assumptions": ["Unknown recipe"],
            },
            usda_lookup={
                "outcome": "no_suitable_match",
                "fallback_reason": "No bakery match in local dataset",
                "lookup_ids": [receipt["lookup_id"]],
            },
        )
        assert food["usda_fdc_id"] is None
        assert food["source_evidence"]["0"]["usda_fdc_id"] == 123
    assert repository.inventory.get_food(canonical["food_id"])["source_evidence"]


def test_migration_leaves_existing_history_unlinked(tmp_path, meal):
    import sqlite3

    from mcp_nutrition_db.repository import MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4

    database = tmp_path / "v4.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        for number, migration in enumerate([MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4], 1):
            connection.executescript(migration)
            connection.execute("INSERT INTO schema_migrations VALUES (?, '2026-01-01')", (number,))
        connection.execute(
            "INSERT INTO entries VALUES ('entry',1,'2026-08-27T12:00:00Z',"
            "'2026-08-27T12:00:00Z','Europe/Zurich','lunch','Original',NULL,NULL,"
            "'2026-08-27','2026-08-27',NULL)"
        )
        connection.execute(
            "INSERT INTO entry_components(component_id,entry_id,position,name,source_type,"
            "calories_mkcal) VALUES ('component','entry',0,'Old food','estimated',123456)"
        )
    migrated = NutritionRepository(database)
    component = migrated.get_entry("entry")["components"][0]
    assert component["inventory"] is None
    assert component["nutrition"]["calories_kcal"] == 123.456
    assert migrated.inventory.status()["foods"] == {}
