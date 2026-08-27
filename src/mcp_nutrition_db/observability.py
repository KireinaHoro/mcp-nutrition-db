"""Structured, deliberately redacted application logging."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Protocol


class RequestContext(Protocol):
    @property
    def request_id(self) -> str: ...


class JsonFormatter(logging.Formatter):
    """Format only an explicit allowlist of operational fields."""

    fields = (
        "tool",
        "outcome",
        "duration_ms",
        "correlation_id",
        "mcp_request_id",
        "error_type",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    package_logger = logging.getLogger("mcp_nutrition_db")
    package_logger.handlers = [handler]
    package_logger.setLevel(level.upper())
    package_logger.propagate = False


@contextmanager
def logged_tool_call(tool: str, context: RequestContext) -> Iterator[None]:
    """Log one tool outcome without serializing arguments or returned data."""

    started = time.perf_counter()
    correlation_id = uuid.uuid4().hex
    common: dict[str, object] = {
        "tool": tool,
        "correlation_id": correlation_id,
        "mcp_request_id": context.request_id,
    }
    logger = logging.getLogger("mcp_nutrition_db.tool")
    try:
        yield
    except Exception as error:
        logger.warning(
            "mcp_tool_call",
            extra={
                **common,
                "outcome": "error",
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
                "error_type": type(error).__name__,
            },
        )
        raise
    else:
        logger.info(
            "mcp_tool_call",
            extra={
                **common,
                "outcome": "success",
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
            },
        )
