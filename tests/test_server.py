from __future__ import annotations

import asyncio

import httpx

from mcp_nutrition_db.repository import NutritionRepository
from mcp_nutrition_db.server import create_server


def test_server_advertises_fourteen_tools_with_safe_annotations(
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
    }
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
            assert policy.json()["result"]["structuredContent"]["policy_id"] == "energy-credit/v1"

    asyncio.run(exercise())
