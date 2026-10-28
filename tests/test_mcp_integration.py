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
            assert initialized.server_info.name == "mcp-nutrition-db"
            assert "durable nutrition record" in (initialized.instructions or "")

            tools = await session.list_tools()
            assert len(tools.tools) == 35
            descriptions = {tool.name: tool.description for tool in tools.tools}
            assert "ACTIVE" in descriptions["nutrition_get_training"]
            assert "garmin_activity" in descriptions["nutrition_list_trainings"]

            status = await session.call_tool("nutrition_get_sync_status", {})
            assert status.is_error is False
            assert status.structured_content["connection_hint"]["eligible"] is False
            weight = await session.call_tool("nutrition_get_body_weight", {})
            assert weight.structured_content["measurement"] is None
            assert weight.structured_content["weight_budget_review"]["weight_stale"] is True
            review = await session.call_tool("nutrition_get_weight_budget_review", {})
            assert review.is_error is False
            assert "goals" in review.structured_content
            history = await session.call_tool("nutrition_list_body_measurements", {"limit": 1})
            assert history.structured_content["measurements"] == []
            reminder = await session.call_tool(
                "nutrition_update_weight_budget_review_reminder",
                {
                    "action": "enable",
                    "expected_revision": 0,
                },
            )
            assert reminder.is_error is False
            assert reminder.structured_content["enabled"] is True

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
            assert logged.is_error is False
            assert logged.structured_content is not None
            assert logged.structured_content["totals"]["calories_kcal"] == 130

            entry = logged.structured_content
            corrected = await session.call_tool(
                "nutrition_update_entry",
                {
                    "entry_id": entry["entry_id"],
                    "expected_revision": entry["revision"],
                    "reason": "Correct portion and retain original component",
                    "changes": {
                        "components": [
                            {"existing_component_id": entry["components"][0]["component_id"]},
                            {
                                "name": "Additional yogurt",
                                "source": {"type": "user_provided"},
                                "nutrition": {"calories_kcal": 65},
                            },
                        ],
                    },
                },
            )
            assert corrected.is_error is False
            assert corrected.structured_content["revision"] == entry["revision"] + 1
            assert corrected.structured_content["totals"]["calories_kcal"] == 195
            fetched = await session.call_tool(
                "nutrition_get_entry", {"entry_id": entry["entry_id"]}
            )
            assert fetched.structured_content["totals"]["calories_kcal"] == 195

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
            assert listed.is_error is False
            assert listed.structured_content is not None
            assert len(listed.structured_content["entries"]) == 1

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
            assert goal.is_error is False
            proposal = await session.call_tool(
                "nutrition_propose_weight_budget_review",
                {
                    "proposal": {
                        "outcome": "keep",
                        "measurement_start": "2026-08-01",
                        "measurement_end": "2026-08-27",
                        "rationale": "Synthetic review",
                    }
                },
            )
            assert proposal.is_error is False
            proposal_id = proposal.structured_content["proposal_id"]
            denied = await session.call_tool(
                "nutrition_complete_weight_budget_review",
                {
                    "proposal_id": proposal_id,
                    "user_approved": False,
                },
            )
            assert denied.is_error is True
            approved = await session.call_tool(
                "nutrition_complete_weight_budget_review",
                {
                    "proposal_id": proposal_id,
                    "user_approved": True,
                },
            )
            assert approved.is_error is False
            assert approved.structured_content["outcome"] == "keep"

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
            assert training.is_error is False
            assert training.structured_content is not None
            assert training.structured_content["calorie_basis"] == "active"
            assert training.structured_content["reported_burn_kcal"] == 850
            assert training.structured_content["credited_burn_kcal"] == 850

            policy = await session.call_tool("nutrition_get_energy_policy", {})
            assert policy.is_error is False
            assert policy.structured_content is not None
            assert policy.structured_content["policy_id"] == "energy-credit/v7"

            review = await session.call_tool(
                "nutrition_get_activity_plan",
                {
                    "on_date": "2026-08-27",
                },
            )
            assert not review.is_error
            assert review.structured_content["activity_plan"] is None
            reviewed = await session.call_tool(
                "nutrition_set_activity_plan",
                {
                    "on_date": "2026-08-27",
                    "exceptional_activity": True,
                    "reason": "Planned exceptional activity",
                },
            )
            assert not reviewed.is_error
            assert reviewed.structured_content["record"]["revision"] == 1
            review = await session.call_tool(
                "nutrition_get_activity_plan",
                {
                    "on_date": "2026-08-27",
                },
            )
            assert not review.is_error
            assert review.structured_content["activity_plan"]["revision"] == 1
            conflict = await session.call_tool(
                "nutrition_set_activity_plan",
                {
                    "on_date": "2026-08-27",
                    "exceptional_activity": True,
                    "reason": "Stale revision",
                },
            )
            assert conflict.is_error

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
            assert summary.is_error is False
            assert summary.structured_content is not None
            group = summary.structured_content["groups"][0]
            assert group["trainings"][0]["calorie_basis"] == "active"
            assert group["reported_training_burn_kcal"] == 850
            assert group["credited_training_burn_kcal"] == 850
            assert group["energy_balance"]["available_ceiling_kcal"] == 2650

    asyncio.run(exercise())
