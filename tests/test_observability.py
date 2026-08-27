from __future__ import annotations

import json
import logging

import pytest

from mcp_nutrition_db.observability import JsonFormatter, logged_tool_call


class FakeContext:
    request_id = "request-17"


def test_tool_log_is_structured_and_redacts_error_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO, logger="mcp_nutrition_db.tool"),
        pytest.raises(ValueError, match="private meal note"),
        logged_tool_call("nutrition_summarize", FakeContext()),
    ):
        raise ValueError("private meal note")

    record = caplog.records[-1]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "mcp_tool_call"
    assert payload["tool"] == "nutrition_summarize"
    assert payload["outcome"] == "error"
    assert payload["mcp_request_id"] == "request-17"
    assert payload["error_type"] == "ValueError"
    assert isinstance(payload["correlation_id"], str)
    assert payload["duration_ms"] >= 0
    assert "private meal note" not in json.dumps(payload)
