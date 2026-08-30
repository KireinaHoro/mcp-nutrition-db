from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_mcp_initialize_list_and_call(tmp_path: Path) -> None:
    database = tmp_path / "mcp.sqlite3"

    async def exercise() -> None:
        environment = dict(os.environ)
        source_path = str(Path.cwd() / "src")
        inherited_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            source_path
            if not inherited_python_path
            else f"{source_path}{os.pathsep}{inherited_python_path}"
        )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "mcp_nutrition_db",
                "serve",
                "--transport",
                "stdio",
                "--database",
                str(database),
            ],
            env=environment,
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "mcp-nutrition-db"
            assert "durable nutrition record" in (initialized.instructions or "")

            tools = await session.list_tools()
            assert len(tools.tools) == 14

            logged = await session.call_tool(
                "nutrition_log_entry",
                {
                    "occurred_at": "2026-08-27T08:00:00+02:00",
                    "kind": "breakfast",
                    "title": "Yogurt",
                    "components": [
                        {
                            "name": "Plain yogurt",
                            "quantity": 200,
                            "unit": "g",
                            "source": {
                                "type": "nutrition_label",
                                "detail": "Container label",
                            },
                            "nutrition": {
                                "calories_kcal": 130,
                                "protein_g": 10,
                                "carbohydrate_g": 12,
                                "fat_g": 4,
                            },
                        }
                    ],
                },
            )
            assert logged.isError is False
            assert logged.structuredContent is not None
            assert logged.structuredContent["totals"]["calories_kcal"] == 130

            listed = await session.call_tool(
                "nutrition_list_entries",
                {
                    "window": {
                        "type": "calendar_day",
                        "date": "2026-08-27",
                        "timezone": "Europe/Zurich",
                    }
                },
            )
            assert listed.isError is False
            assert listed.structuredContent is not None
            assert len(listed.structuredContent["entries"]) == 1

            goal = await session.call_tool(
                "nutrition_set_goals",
                {
                    "effective_from": "2026-08-01",
                    "base_burn_kcal": 2200,
                    "deficit_kcal": 400,
                    "targets": {"protein_g": 120},
                    "reason": "Test energy budget",
                },
            )
            assert goal.isError is False

            training = await session.call_tool(
                "nutrition_log_training",
                {
                    "occurred_at": "2026-08-27T18:00:00+02:00",
                    "activity": "Cycling",
                    "duration_minutes": 60,
                    "reported_burn_kcal": 850,
                    "confidence": "high",
                    "measurement_method": "power_meter",
                    "source": {"type": "user_provided", "detail": "Cycling computer"},
                },
            )
            assert training.isError is False
            assert training.structuredContent is not None
            assert training.structuredContent["reported_burn_kcal"] == 850
            assert training.structuredContent["credited_burn_kcal"] == 850

            policy = await session.call_tool("nutrition_get_energy_policy", {})
            assert policy.isError is False
            assert policy.structuredContent is not None
            assert policy.structuredContent["policy_id"] == "energy-credit/v2"

            summary = await session.call_tool(
                "nutrition_summarize",
                {
                    "window": {
                        "type": "calendar_day",
                        "date": "2026-08-27",
                        "timezone": "Europe/Zurich",
                    }
                },
            )
            assert summary.isError is False
            assert summary.structuredContent is not None
            group = summary.structuredContent["groups"][0]
            assert group["reported_training_burn_kcal"] == 850
            assert group["credited_training_burn_kcal"] == 850
            assert group["energy_balance"]["available_ceiling_kcal"] == 2650

    asyncio.run(exercise())
