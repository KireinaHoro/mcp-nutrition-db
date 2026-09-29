from __future__ import annotations

import asyncio

import httpx

from mcp_nutrition_db.repository import NutritionRepository
from mcp_nutrition_db.server import create_server


def test_server_advertises_inventory_tools_with_safe_annotations(
    repository: NutritionRepository,
) -> None:
    server = create_server(repository)
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert set(tools) == {
        "nutrition_log_entry",
        "nutrition_get_entry",
        "nutrition_update_entry",
        "nutrition_delete_entry",
        "nutrition_list_entries",
        "nutrition_summarize",
        "nutrition_set_goals",
        "nutrition_get_goals",
        "nutrition_log_training",
        "nutrition_get_training",
        "nutrition_update_training",
        "nutrition_delete_training",
        "nutrition_list_trainings",
        "nutrition_get_energy_policy",
        "nutrition_get_activity_plan",
        "nutrition_set_activity_plan",
        "nutrition_inventory_status",
        "nutrition_search_foods",
        "nutrition_get_food",
        "nutrition_create_food",
        "nutrition_update_food",
        "nutrition_archive_food",
        "nutrition_resolve_food",
        "nutrition_find_food_matches",
        "nutrition_preview_food_links",
        "nutrition_apply_food_links",
        "nutrition_search_usda_foods",
        "nutrition_get_usda_food",
    }
    assert tools["nutrition_search_usda_foods"].annotations.openWorldHint is False
    assert tools["nutrition_get_usda_food"].annotations.openWorldHint is False
    assert tools["nutrition_get_entry"].annotations.readOnlyHint is True
    assert tools["nutrition_list_entries"].annotations.readOnlyHint is True
    assert tools["nutrition_delete_entry"].annotations.destructiveHint is True
    assert tools["nutrition_log_entry"].annotations.idempotentHint is False
    assert tools["nutrition_list_trainings"].annotations.readOnlyHint is True
    assert tools["nutrition_delete_training"].annotations.destructiveHint is True
    assert tools["nutrition_get_energy_policy"].annotations.readOnlyHint is True


def test_schema_exposes_relative_day_and_component_provenance(
    repository: NutritionRepository,
) -> None:
    server = create_server(repository)
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    list_schema = tools["nutrition_list_entries"].parameters
    assert "window" in list_schema["properties"]
    assert "relative_day" in str(list_schema)
    log_schema = tools["nutrition_log_entry"].parameters
    assert "source" in str(log_schema)
    assert "nutrition_label" in str(log_schema)
    training_schema = tools["nutrition_log_training"].parameters
    assert "reported_burn_kcal" in str(training_schema)
    assert "power_meter" in str(training_schema)
    assert "confidence" in str(training_schema)
    plan_schema = tools["nutrition_set_activity_plan"].parameters
    assert "intake_complete" not in str(plan_schema)
    assert "exceptional_activity" in plan_schema["required"]


def test_streamable_http_initializes_and_calls_policy(
    repository: NutritionRepository,
) -> None:
    server = create_server(repository)
    app = server.streamable_http_app()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as client,
        ):
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            }
            initialized = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "http-test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200
            assert initialized.json()["result"]["serverInfo"]["name"] == "mcp-nutrition-db"

            policy = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "nutrition_get_energy_policy", "arguments": {}},
                },
            )
            assert policy.status_code == 200
            structured = policy.json()["result"]["structuredContent"]
            assert structured["policy_id"] == "energy-credit/v6"
            assert structured["recovery_pool_cap"] == (
                "next_day_planned_deficit / first_recovery_weight"
            )

    asyncio.run(exercise())


def test_http_lifecycle_partial_summary_and_revision_errors(repository, meal_payload):
    import json

    app = create_server(repository).streamable_http_app()

    async def exercise():
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787"
            ) as client,
        ):
            sequence = 0

            async def call(name, arguments):
                nonlocal sequence
                sequence += 1
                response = await client.post(
                    "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": sequence,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                assert response.status_code == 200
                return response.json()["result"]

            created = await call("nutrition_log_entry", meal_payload)
            entry = created["structuredContent"]
            updated = await call(
                "nutrition_update_entry",
                {
                    "entry_id": entry["entry_id"],
                    "expected_revision": 1,
                    "reason": "HTTP lifecycle",
                    "changes": {"notes": None},
                },
            )
            assert updated["structuredContent"]["revision"] == 2
            assert updated["structuredContent"]["notes"] is None
            stale = await call(
                "nutrition_update_entry",
                {
                    "entry_id": entry["entry_id"],
                    "expected_revision": 1,
                    "reason": "Stale request",
                    "changes": {"notes": "stale"},
                },
            )
            assert stale["isError"]
            error = json.loads(
                stale["content"][0]["text"].removeprefix(
                    "Error executing tool nutrition_update_entry: "
                )
            )
            assert error["code"] == "revision_conflict"
            assert error["current_revision"] == 2
            invalid = await call(
                "nutrition_update_entry",
                {
                    "entry_id": entry["entry_id"],
                    "expected_revision": 2,
                    "reason": "Invalid null",
                    "changes": {"title": None},
                },
            )
            assert invalid["isError"]
            assert repository.get_entry(entry["entry_id"])["revision"] == 2
            summary = await call(
                "nutrition_summarize",
                {
                    "window": {"type": "calendar_day", "date": "2026-08-27"},
                },
            )
            assert not summary["structuredContent"]["groups"][0]["completeness"]["carbohydrate_g"][
                "complete"
            ]
            deleted = await call(
                "nutrition_delete_entry",
                {
                    "entry_id": entry["entry_id"],
                    "expected_revision": 2,
                    "reason": "HTTP cleanup",
                },
            )
            assert deleted["structuredContent"]["deleted"]

    asyncio.run(exercise())
