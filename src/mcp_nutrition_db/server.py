"""FastMCP tool surface for the nutrition repository."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .models import (
    DEFAULT_TIMEZONE,
    ComponentInput,
    EntryChanges,
    EntryKind,
    Estimation,
    GoalInput,
    ListEntriesInput,
    NutritionValues,
    QueryWindow,
    SummarizeInput,
    validate_timezone,
)
from .repository import NutritionRepository, RepositoryError

INSTRUCTIONS = """Use these tools as the durable nutrition record. ChatGPT interprets meal
photos and conversation; this server validates and stores the resulting structured estimates.
Use nutrition_log_entry once for a new meal, then nutrition_update_entry when the user corrects
portions or ingredients. Preserve per-component source provenance and uncertainty. For requests
about today, pass a relative_day window instead of calculating timestamps. Nutrition estimates
are not medical advice."""

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


def _translate_error(error: Exception) -> ToolError:
    if isinstance(error, RepositoryError | ValueError):
        return ToolError(str(error))
    return ToolError("nutrition database operation failed")


def create_server(
    repository: NutritionRepository,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    default_timezone: str = DEFAULT_TIMEZONE,
) -> FastMCP:
    validate_timezone(default_timezone)
    server = FastMCP(
        "mcp-nutrition-db",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

    def window_with_default(window: QueryWindow) -> QueryWindow:
        if "timezone" not in window.model_fields_set:
            return window.model_copy(update={"timezone": default_timezone})
        return window

    @server.custom_route(  # type: ignore[untyped-decorator]
        "/healthz", methods=["GET"], include_in_schema=False
    )
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "schema_version": repository.schema_version()},
            headers={"Cache-Control": "no-store"},
        )

    @server.tool(
        name="nutrition_log_entry",
        description=(
            "Log one new meal or snack. Provide structured nutrition for every component and "
            "identify whether each value was estimated, label-derived, restaurant-declared, "
            "or from another source. Exact retries within ten minutes return the original entry."
        ),
        annotations=MUTATING,
    )
    def nutrition_log_entry(
        occurred_at: datetime,
        kind: EntryKind,
        title: Annotated[str, Field(min_length=1, max_length=300)],
        components: Annotated[list[ComponentInput], Field(min_length=1, max_length=100)],
        timezone: str = default_timezone,
        notes: Annotated[str | None, Field(max_length=5_000)] = None,
        estimation: Estimation | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        try:
            from .models import LogEntryInput

            return repository.create_entry(
                LogEntryInput(
                    occurred_at=occurred_at,
                    kind=kind,
                    title=title,
                    components=components,
                    timezone=timezone,
                    notes=notes,
                    estimation=estimation,
                    force_new=force_new,
                )
            )
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_get_entry",
        description="Fetch one complete active nutrition entry by its entry_id.",
        annotations=READ_ONLY,
    )
    def nutrition_get_entry(entry_id: str) -> dict[str, Any]:
        try:
            return repository.get_entry(entry_id)
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_update_entry",
        description=(
            "Correct an existing entry after learning new portion, ingredient, timing, or "
            "provenance information. Supply the last observed revision; components replace the "
            "complete component list when provided."
        ),
        annotations=MUTATING,
    )
    def nutrition_update_entry(
        entry_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        changes: EntryChanges,
    ) -> dict[str, Any]:
        try:
            return repository.update_entry(entry_id, expected_revision, reason, changes)
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_delete_entry",
        description="Soft-delete one entry. Requires its last observed revision and a reason.",
        annotations=DESTRUCTIVE,
    )
    def nutrition_delete_entry(
        entry_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
    ) -> dict[str, Any]:
        try:
            return repository.delete_entry(entry_id, expected_revision, reason)
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_list_entries",
        description=(
            "List meals and snacks in a bounded window. For today, use "
            '{"type":"relative_day","day":"today"}; the server resolves timezone boundaries.'
        ),
        annotations=READ_ONLY,
    )
    def nutrition_list_entries(
        window: QueryWindow,
        kind: EntryKind | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        try:
            return repository.list_entries(
                ListEntriesInput(
                    window=window_with_default(window),
                    kind=kind,
                    cursor=cursor,
                    limit=limit,
                )
            )
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_summarize",
        description=(
            "Sum nutrition over a bounded window, grouped by day or whole range. For today's "
            "macros, use a relative_day window; resolved timestamps are returned."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_summarize(
        window: QueryWindow,
        grouping: Literal["day", "whole_range"] = "whole_range",
    ) -> dict[str, Any]:
        try:
            return repository.summarize(
                SummarizeInput(window=window_with_default(window), grouping=grouping)
            )
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_set_goals",
        description=(
            "Set or replace an effective-dated calorie or macro goal version. Historical dates "
            "continue using the goal version effective on that day."
        ),
        annotations=MUTATING,
    )
    def nutrition_set_goals(
        effective_from: date,
        targets: NutritionValues,
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        timezone: str = default_timezone,
    ) -> dict[str, Any]:
        try:
            return repository.set_goals(
                GoalInput(
                    effective_from=effective_from,
                    timezone=timezone,
                    targets=targets,
                    reason=reason,
                )
            )
        except Exception as error:
            raise _translate_error(error) from error

    @server.tool(
        name="nutrition_get_goals",
        description=(
            "Get the nutrition goal effective on a date. Omit on_date for today's goal; optionally "
            "include all configured goal versions."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_get_goals(
        on_date: date | None = None,
        timezone: str = default_timezone,
        include_history: bool = True,
    ) -> dict[str, Any]:
        try:
            return repository.get_goals(
                on_date=on_date, timezone=timezone, include_history=include_history
            )
        except Exception as error:
            raise _translate_error(error) from error

    return server
