from __future__ import annotations

import json
import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mcp_nutrition_db.errors import InventoryError, RevisionConflictError
from mcp_nutrition_db.models import EntryChanges
from mcp_nutrition_db.tool_support import tool_call


class Context:
    request_id = "review-test"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (RevisionConflictError("entry", 1, 2), "revision_conflict"),
        (
            InventoryError("revision_conflict", food_id="food", current_revision=2),
            "revision_conflict",
        ),
        (ValueError("invalid cursor"), "validation_error"),
        (RuntimeError("private internal detail"), "internal_error"),
    ],
)
def test_tool_errors_are_structured_and_log_original_class(error, code, caplog):
    with (
        caplog.at_level(logging.INFO, logger="mcp_nutrition_db.tool"),
        pytest.raises(ToolError) as caught,
        tool_call("test", Context()),
    ):
        raise error
    payload = json.loads(str(caught.value))
    assert payload["code"] == code
    assert "private internal detail" not in str(caught.value)
    assert len(caplog.records) == 1
    assert caplog.records[0].error_type == type(error).__name__
    if code == "revision_conflict":
        assert payload["current_revision"] == 2


def test_validation_error_keeps_locations_without_echoing_inputs():
    with pytest.raises(ToolError) as caught, tool_call("test", Context()):
        EntryChanges(title="private" * 100)
    payload = json.loads(str(caught.value))
    assert payload["code"] == "validation_error"
    assert payload["errors"][0]["loc"] == ["title"]
    assert "private" not in str(caught.value)
