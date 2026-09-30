"""FastMCP tool surface for the nutrition repository."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .inventory_server import INVENTORY_INSTRUCTIONS, register_inventory_tools
from .models import (
    DEFAULT_TIMEZONE,
    ActivityPlanInput,
    Confidence,
    EntryChanges,
    EntryKind,
    Estimation,
    GoalInput,
    ListEntriesInput,
    ListTrainingsInput,
    LogTrainingInput,
    MealComponentInput,
    NutritionValues,
    QueryWindow,
    SummarizeInput,
    TrainingChanges,
    TrainingEvidence,
    TrainingMeasurementMethod,
    TrainingSource,
    validate_timezone,
)
from .repository import NutritionRepository
from .tool_support import (
    DESTRUCTIVE,
    MUTATING,
    READ_ONLY,
    MCPContext,
    tool_call,
    window_with_default,
)
from .weight_server import WEIGHT_INSTRUCTIONS, register_weight_tools

INSTRUCTIONS = """Calorie accounting distinguishes the ordinary target, incoming recovery
allowance, and confidence-adjusted exercise allowance. An allowance is an optional ceiling, not
a recommendation to eat it. Use server-returned energy calculations; call
nutrition_get_energy_policy when explaining the policy or proposing a change. Use these tools as
the durable nutrition record. ChatGPT interprets meal photos and conversation; this server
validates and stores the resulting structured estimates.
Use nutrition_log_entry once for a new meal, then nutrition_update_entry when the user corrects
portions or ingredients. Preserve per-component source provenance and uncertainty. For requests
about today, pass a relative_day window instead of calculating timestamps. Preserve training
measurement method, evidence, and confidence. Outstanding surplus produces a bounded additional
deficit on subsequent eligible days. Repeated overshoots extend repayment without increasing the
daily adjustment. New debit is intake above base burn plus credited exercise plus incoming
recovery. Incoming recovery and forgiveness of the missed ordinary deficit are additive;
recovery does not increase estimated same-day expenditure. Do not classify overshoots or ask
about travel or return dates for accounting.
Exceptional (extraordinary) activity days DO repay opening debit from unused budget after
reserving the capped protected recovery pool. Activity and recovery pauses apply only to
requested additional restriction, never to observed repayment. Zero additional_deficit_kcal
does not imply zero repaid_kcal. Settled repayment can exceed 200 kcal, up to opening debit.
Past local days with logged intake and known calories settle automatically when queried.
No daily confirmation or scheduled closing event is needed. Today's accounting is provisional;
backdated additions, edits, and deletions recalculate subsequent balances. Empty days and days
with unknown calories cannot repay debit. Use nutrition_set_activity_plan for optional planned
exceptional activity before it occurs. Fuel demanding exercise and recovery first; debit is
bookkeeping, not an instruction to under-fuel. Use the server's capped additional deficit and
projected eligible days rather than inventing repayment targets or deadlines.
Nutrition and exercise estimates are not medical advice."""


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
        instructions=INSTRUCTIONS + INVENTORY_INSTRUCTIONS + WEIGHT_INSTRUCTIONS,
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )

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
            "Log one new meal or snack. Prefer inventory references (food_id, food_revision, "
            "amount) for ordinary components, including home-cooked ingredients. Search the "
            "catalog first. Inline nutrition is for idiosyncratic exceptions with provenance. "
            "Exact retries within ten minutes return the original entry."
        ),
        annotations=MUTATING,
    )
    def nutrition_log_entry(
        occurred_at: datetime,
        kind: EntryKind,
        title: Annotated[str, Field(min_length=1, max_length=300)],
        components: Annotated[list[MealComponentInput], Field(min_length=1, max_length=100)],
        ctx: MCPContext,  # type: ignore[type-arg]
        timezone: str = default_timezone,
        notes: Annotated[str | None, Field(max_length=5_000)] = None,
        estimation: Estimation | None = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        with tool_call("nutrition_log_entry", ctx):
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

    @server.tool(
        name="nutrition_get_entry",
        description="Fetch one complete active nutrition entry by its entry_id.",
        annotations=READ_ONLY,
    )
    def nutrition_get_entry(
        entry_id: str,
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_get_entry", ctx):
            return repository.get_entry(entry_id)

    @server.tool(
        name="nutrition_update_entry",
        description=(
            "Correct an existing entry after learning new portion, ingredient, timing, or "
            "provenance information. Supply the last observed revision; components replace the "
            "complete component list when provided. Each component is either inline "
            "{name, source, nutrition, optional quantity and unit}, an inventory reference "
            "{food_id, food_revision, amount}, or {existing_component_id} to retain an "
            "unchanged component. Fetch the entry first for current IDs and revision."
        ),
        annotations=MUTATING,
    )
    def nutrition_update_entry(
        entry_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        changes: EntryChanges,
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_update_entry", ctx):
            return repository.update_entry(entry_id, expected_revision, reason, changes)

    @server.tool(
        name="nutrition_delete_entry",
        description="Soft-delete one entry. Requires its last observed revision and a reason.",
        annotations=DESTRUCTIVE,
    )
    def nutrition_delete_entry(
        entry_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_delete_entry", ctx):
            return repository.delete_entry(entry_id, expected_revision, reason)

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
        ctx: MCPContext,  # type: ignore[type-arg]
        kind: EntryKind | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        with tool_call("nutrition_list_entries", ctx):
            return repository.list_entries(
                ListEntriesInput(
                    window=window_with_default(window, default_timezone),
                    kind=kind,
                    cursor=cursor,
                    limit=limit,
                )
            )

    @server.tool(
        name="nutrition_log_training",
        description=(
            "Log one training session and its reported ACTIVE energy-burn estimate, excluding "
            "resting calories. Classify confidence "
            "and measurement method and include supporting evidence when available. The server "
            "preserves reported burn and calculates policy-adjusted credited burn. Exact retries "
            "within ten minutes return the original training."
        ),
        annotations=MUTATING,
    )
    def nutrition_log_training(
        occurred_at: datetime,
        activity: Annotated[str, Field(min_length=1, max_length=200)],
        duration_minutes: Annotated[float, Field(gt=0, le=10_080)],
        reported_burn_kcal: Annotated[float, Field(gt=0, le=100_000)],
        confidence: Confidence,
        measurement_method: TrainingMeasurementMethod,
        source: TrainingSource,
        ctx: MCPContext,  # type: ignore[type-arg]
        evidence: TrainingEvidence | None = None,
        timezone: str = default_timezone,
        notes: Annotated[str | None, Field(max_length=5_000)] = None,
        force_new: bool = False,
    ) -> dict[str, Any]:
        with tool_call("nutrition_log_training", ctx):
            return repository.create_training(
                LogTrainingInput(
                    occurred_at=occurred_at,
                    activity=activity,
                    duration_minutes=duration_minutes,
                    reported_burn_kcal=reported_burn_kcal,
                    confidence=confidence,
                    measurement_method=measurement_method,
                    source=source,
                    evidence=evidence,
                    timezone=timezone,
                    notes=notes,
                    force_new=force_new,
                )
            )

    @server.tool(
        name="nutrition_get_training",
        description=(
            "Fetch one complete active training by its training_id. Garmin-linked trainings "
            "include garmin_activity with available distance, heart-rate, power and other "
            "metrics with explicit units, plus the active/total/resting calorie breakdown. "
            "reported_burn_kcal is ACTIVE calories, excluding resting calories; "
            "credited_burn_kcal applies confidence once. Garmin details are latest source "
            "measurements; check sync_status for pending changes and retain local overrides."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_get_training(
        training_id: str,
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_get_training", ctx):
            return repository.get_training(training_id)

    @server.tool(
        name="nutrition_update_training",
        description=(
            "Correct a training's activity, timing, reported burn, confidence, measurement "
            "method, evidence, or source. Supply the last observed revision to avoid overwriting "
            "a newer correction. Derived credit and affected recovery days are recalculated."
        ),
        annotations=MUTATING,
    )
    def nutrition_update_training(
        training_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        changes: TrainingChanges,
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_update_training", ctx):
            return repository.update_training(training_id, expected_revision, reason, changes)

    @server.tool(
        name="nutrition_delete_training",
        description="Soft-delete one training. Requires its last observed revision and a reason.",
        annotations=DESTRUCTIVE,
    )
    def nutrition_delete_training(
        training_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        with tool_call("nutrition_delete_training", ctx):
            return repository.delete_training(training_id, expected_revision, reason)

    @server.tool(
        name="nutrition_list_trainings",
        description=(
            "List training sessions with available Garmin distance, HR, power and other metrics "
            "in garmin_activity. Reported burn is per-activity ACTIVE calories, excluding "
            "resting calories; credited burn applies confidence once. For today, use "
            '{"type":"relative_day","day":"today"}; resolved boundaries are returned.'
        ),
        annotations=READ_ONLY,
    )
    def nutrition_list_trainings(
        window: QueryWindow,
        ctx: MCPContext,  # type: ignore[type-arg]
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        with tool_call("nutrition_list_trainings", ctx):
            return repository.list_trainings(
                ListTrainingsInput(
                    window=window_with_default(window, default_timezone), cursor=cursor, limit=limit
                )
            )

    @server.tool(
        name="nutrition_summarize",
        description=(
            "Sum nutrition over a bounded window, grouped by day or whole range. Includes "
            "training details and available Garmin metrics. Training burn uses ACTIVE "
            "calories excluding resting calories, with confidence applied once to credit. "
            "For today's macros and energy balance, use a relative_day window. Energy results "
            "distinguish "
            "ordinary target, protected recovery, exercise allowance, and debit adjustment. "
            "Exceptional-day unused budget repays debit after reserving capped recovery; "
            "only additional restriction is paused. Read debit.repaid_kcal and unsettled_dates "
            "when reporting repayment."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_summarize(
        window: QueryWindow,
        ctx: MCPContext,  # type: ignore[type-arg]
        grouping: Literal["day", "whole_range"] = "whole_range",
    ) -> dict[str, Any]:
        with tool_call("nutrition_summarize", ctx):
            return repository.summarize(
                SummarizeInput(
                    window=window_with_default(window, default_timezone), grouping=grouping
                )
            )

    @server.tool(
        name="nutrition_set_goals",
        description=(
            "For explicit goal requests unrelated to weight reviews. Weight-driven "
            "changes MUST use "
            "the weight-budget proposal/completion workflow after explicit approval. "
            "Set an effective-dated base daily burn, calorie deficit, and optional macro targets. "
            "The server derives the ordinary target and separate recovery and exercise allowances."
        ),
        annotations=MUTATING,
    )
    def nutrition_set_goals(
        effective_from: date,
        base_burn_kcal: Annotated[float, Field(gt=0, le=100_000)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
        deficit_kcal: Annotated[float, Field(ge=0, le=100_000)] = 0,
        targets: NutritionValues | None = None,
        timezone: str = default_timezone,
    ) -> dict[str, Any]:
        with tool_call("nutrition_set_goals", ctx):
            return repository.set_goals(
                GoalInput(
                    effective_from=effective_from,
                    timezone=timezone,
                    base_burn_kcal=base_burn_kcal,
                    deficit_kcal=deficit_kcal,
                    targets=targets,
                    reason=reason,
                )
            )

    @server.tool(
        name="nutrition_get_goals",
        description=(
            "Get the nutrition goal and server-calculated energy balance effective on a date. "
            "Omit on_date for today; optionally include all configured goal versions. "
            "Nutrient completeness is not day completion."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_get_goals(
        ctx: MCPContext,  # type: ignore[type-arg]
        on_date: date | None = None,
        timezone: str = default_timezone,
        include_history: bool = True,
    ) -> dict[str, Any]:
        with tool_call("nutrition_get_goals", ctx):
            return repository.get_goals(
                on_date=on_date, timezone=timezone, include_history=include_history
            )

    @server.tool(
        name="nutrition_get_energy_policy",
        description=(
            "Return the active energy, recovery, and surplus policy. Call this when "
            "explaining debit, activity pauses, allowances, confidence, or "
            "expiry; calculations in summaries and goals are already performed by the server."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_get_energy_policy(ctx: MCPContext) -> dict[str, Any]:  # type: ignore[type-arg]
        with tool_call("nutrition_get_energy_policy", ctx):
            return repository.energy_policy()

    @server.tool(
        name="nutrition_get_activity_plan",
        description=(
            "Read the selected day's optional exceptional-activity plan, including "
            "its revision. Use this before correcting a plan; no record means revision 0."
        ),
        annotations=READ_ONLY,
    )
    def nutrition_get_activity_plan(
        ctx: MCPContext,  # type: ignore[type-arg]
        on_date: date | None = None,
        timezone: str = default_timezone,
    ) -> dict[str, Any]:
        with tool_call("nutrition_get_activity_plan", ctx):
            return repository.get_activity_plan(on_date, timezone)

    @server.tool(
        name="nutrition_set_activity_plan",
        description=(
            "Set or clear an optional planned exceptional activity day to pause extra restriction "
            "before training is logged. No daily intake confirmation is needed. "
            "expected_revision is 0 for a new plan."
        ),
        annotations=MUTATING,
    )
    def nutrition_set_activity_plan(
        on_date: date,
        exceptional_activity: bool,
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
        timezone: str = default_timezone,
        expected_revision: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        with tool_call("nutrition_set_activity_plan", ctx):
            return repository.set_activity_plan(
                ActivityPlanInput(
                    on_date=on_date,
                    exceptional_activity=exceptional_activity,
                    timezone=timezone,
                    expected_revision=expected_revision,
                    reason=reason,
                )
            )

    register_weight_tools(server, repository, default_timezone)
    register_inventory_tools(server, repository, default_timezone)
    return server
