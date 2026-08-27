from __future__ import annotations

from mcp_nutrition_db.repository import NutritionRepository
from mcp_nutrition_db.server import create_server


def test_server_advertises_eight_tools_with_safe_annotations(
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
    }
    assert tools["nutrition_get_entry"].annotations.readOnlyHint is True
    assert tools["nutrition_list_entries"].annotations.readOnlyHint is True
    assert tools["nutrition_delete_entry"].annotations.destructiveHint is True
    assert tools["nutrition_log_entry"].annotations.idempotentHint is False


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
