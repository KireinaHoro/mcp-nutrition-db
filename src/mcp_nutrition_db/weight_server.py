"""Weight history, importer visibility, and explicit conversational review tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .body import BodyRepository
from .repository import NutritionRepository
from .tool_support import MUTATING, READ_ONLY, MCPContext, tool_call
from .weight_review import ReviewProposal, WeightReviewRepository

WEIGHT_INSTRUCTIONS = """
Weight measurements, sync and reminders never authorize calorie or deficit changes.
For any weight-driven budget change, use nutrition_get_weight_budget_review, then
nutrition_propose_weight_budget_review. Present the concrete values, effective date,
measurement window, rationale, uncertainty and historical effects to the user. Only after
explicit conversational approval call nutrition_complete_weight_budget_review for that exact
proposal. A keep-current-goals review also requires approval. Do not bypass this workflow using
nutrition_set_goals; that tool remains available for other explicit goal requests.
Preserve macros unless explicitly included in the approved proposal. Default to next local day;
backdating requires explicit approval of its date and historical effect. Never invent an automatic
weight-to-calorie formula. Report missing/stale measurements and data gaps.
During nutrition conversations, present an eligible weight_budget_review hint naturally, noting
stale or missing weight data. Acknowledge only an actually presented hint with the reminder tool.
If the user says later, snooze seven days by default. Reading status is not acknowledgement.
Also inspect garmin_connection and sync.connection_hint: if eligible, tell the user Garmin is
 disconnected or delayed, data may be incomplete, and how to reconnect. For reauth_required,
ask them to run the private garmin-login app and update the sops session secret; never ask for
passwords, MFA codes or tokens in chat. Do not send outbound messages for either reminder.
"""


def register_weight_tools(server: FastMCP, repository: NutritionRepository, timezone: str) -> None:
    body = BodyRepository(repository.database)
    reviews = WeightReviewRepository(repository)

    @server.tool(annotations=READ_ONLY)
    def nutrition_get_body_weight(
        ctx: MCPContext[Any, Any], as_of: datetime | None = None
    ) -> dict[str, Any]:
        """Newest valid measurement by time, age and sync freshness. Does not change goals."""
        with tool_call("nutrition_get_body_weight", ctx):
            return {**body.latest(as_of), "weight_budget_review": reviews.status(timezone)}

    @server.tool(annotations=READ_ONLY)
    def nutrition_list_body_measurements(
        ctx: MCPContext[Any, Any],
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Paginated weight history in kilograms; preserves separate same-day readings."""
        with tool_call("nutrition_list_body_measurements", ctx):
            return body.list_measurements(start=start, end=end, cursor=cursor, limit=limit)

    @server.tool(annotations=READ_ONLY)
    def nutrition_get_sync_status(ctx: MCPContext[Any, Any]) -> dict[str, Any]:
        """Garmin connection hints, separate stream freshness, coverage and pending records."""
        with tool_call("nutrition_get_sync_status", ctx):
            return body.sync_status()

    @server.tool(annotations=READ_ONLY)
    def nutrition_get_weight_budget_review(ctx: MCPContext[Any, Any]) -> dict[str, Any]:
        """Read review status, recent weights and saved goals without approving changes."""
        with tool_call("nutrition_get_weight_budget_review", ctx):
            return reviews.get(timezone)

    @server.tool(annotations=MUTATING)
    def nutrition_propose_weight_budget_review(
        proposal: ReviewProposal, ctx: MCPContext[Any, Any]
    ) -> dict[str, Any]:
        """Save concrete proposed goals or keep-current outcome for explicit user approval."""
        with tool_call("nutrition_propose_weight_budget_review", ctx):
            return reviews.propose(proposal)

    @server.tool(annotations=MUTATING)
    def nutrition_complete_weight_budget_review(
        proposal_id: str,
        user_approved: bool,
        ctx: MCPContext[Any, Any],
        approved_backdate: date | None = None,
    ) -> dict[str, Any]:
        """Apply this proposal AFTER explicit approval; reject stale goals. Idempotent."""
        with tool_call("nutrition_complete_weight_budget_review", ctx):
            return reviews.complete(
                proposal_id, user_approved=user_approved, approved_backdate=approved_backdate
            )

    @server.tool(annotations=MUTATING)
    def nutrition_update_weight_budget_review_reminder(
        action: Literal["acknowledge", "snooze", "enable", "disable"],
        expected_revision: int,
        ctx: MCPContext[Any, Any],
        until: date | None = None,
    ) -> dict[str, Any]:
        """Acknowledge a hint, snooze (default seven days), enable or disable reminders."""
        with tool_call("nutrition_update_weight_budget_review_reminder", ctx):
            return reviews.update_reminder(action, expected_revision, timezone, until)
