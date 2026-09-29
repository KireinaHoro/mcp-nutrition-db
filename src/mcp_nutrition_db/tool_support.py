"""Shared MCP annotations, timezone defaults, and safe error translation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from .errors import RepositoryError
from .models import QueryWindow
from .observability import RequestContext, logged_tool_call
from .serialization import canonical_json

# The SDK detects only the unparameterized runtime class for context injection.
MCPContext = Context

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
MUTATING = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def window_with_default(window: QueryWindow, timezone: str) -> QueryWindow:
    if "timezone" not in window.model_fields_set:
        return window.model_copy(update={"timezone": timezone})
    return window


@contextmanager
def tool_call(name: str, context: RequestContext) -> Iterator[None]:
    try:
        # Log the original exception type before translating it for MCP.
        with logged_tool_call(name, context):
            yield
    except RepositoryError as error:
        raise ToolError(canonical_json(error.payload())) from error
    except ValidationError as error:
        details = error.errors(include_input=False, include_context=False, include_url=False)
        raise ToolError(canonical_json({"code": "validation_error", "errors": details})) from error
    except ValueError as error:
        raise ToolError(
            canonical_json({"code": "validation_error", "message": str(error)})
        ) from error
    except Exception as error:
        raise ToolError(
            canonical_json(
                {"code": "internal_error", "message": "nutrition database operation failed"}
            )
        ) from error
