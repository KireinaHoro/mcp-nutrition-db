"""MCP inventory tools; mutations remain explicit and revision checked."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .inventory_models import FoodDefinition, FoodLink
from .models import Estimation, FoodAmount, QueryWindow
from .observability import logged_tool_call
from .repository import NutritionRepository
from .server import MUTATING, READ_ONLY, MCPContext, _translate_error
from .usda import USDAClient

INVENTORY_INSTRUCTIONS = """
Use inventory references for ordinary food components, including individual ingredients of
home-cooked meals. Search nutrition_search_foods BEFORE creating any new food; reuse matching
identities across portions, packs, dates, spellings, and aliases. Inspect preparation and variants.
Use a concise stable short_name such as 'coop surimi'. Duplicate normalized short names, USDA
IDs, and product identifiers are rejected deterministically; fetch/reuse the returned food_id.
Do not evade a conflict by inventing a new short name. Add aliases/servings to existing foods.
For new foods needing composition, search USDA then retrieve a suitable record before estimating.
Direct USDA foods require their unique usda_fdc_id and retrieved source snapshot. Exact product
labels remain valid evidence. Only estimate when suitable USDA data is unavailable; record the
lookup receipt/reason, method, confidence, assumptions, and source. A proxy is an estimate and
references the canonical USDA inventory food. Missing nutrients remain unknown.
Pin food_revision and supply amount; the server scales nutrition. Never assume the weight of a
variable pack or mix raw/cooked, bone-in/edible, or drained/as-sold weights. An estimated portion
can use known inventory composition. Only use inline nutrition for truly idiosyncratic or
inseparable dishes; explain the exception. Use existing_component_id to retain unchanged
components during full-list updates. Catalog revisions never silently change past totals.
Historical conversion uses find_food_matches, individual identity review, preview_food_links,
then apply_food_links. These links preserve original nutrition/provenance; they are not corrections.
"""


def register_inventory_tools(
    server: FastMCP, repository: NutritionRepository, default_timezone: str
) -> None:
    inventory = repository.inventory
    usda = USDAClient(repository)

    def invoke(name: str, ctx: Any, operation: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        with logged_tool_call(name, ctx):
            try:
                result: dict[str, Any] = operation(*args, **kwargs)
                return result
            except Exception as error:
                raise _translate_error(error) from error

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get full active-history date bounds and inventory coverage counts "
            "to verify a complete scrub."
        ),
    )
    def nutrition_inventory_status(
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke("nutrition_inventory_status", ctx, inventory.status)

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Search the reusable catalog BEFORE creating a food. Reuse matching "
            "short names, USDA IDs, products, and ingredients; differing portions "
            "are not new foods. Returns pinned revisions, servings, nutrition, and "
            "source evidence."
        ),
    )
    def nutrition_search_foods(
        ctx: MCPContext,  # type: ignore[type-arg]
        query: str | None = None,
        status: Literal["active", "archived", "all"] = "active",
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_search_foods", ctx, inventory.search_foods, query, status, cursor, limit
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Get a food and its source evidence, optionally at an immutable historical revision."
        ),
    )
    def nutrition_get_food(
        food_id: str,
        ctx: MCPContext,  # type: ignore[type-arg]
        revision: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        return invoke("nutrition_get_food", ctx, inventory.get_food, food_id, revision)

    @server.tool(
        annotations=MUTATING,
        description=(
            "Create a reusable ingredient/product AFTER searching the catalog. "
            "Prefer retrieved USDA composition; estimates require fallback "
            "evidence. Unique USDA IDs and normalized short names reject duplicates"
            " and return the existing item. No override."
        ),
    )
    def nutrition_create_food(
        food: FoodDefinition,
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke("nutrition_create_food", ctx, inventory.create_food, food)

    @server.tool(
        annotations=MUTATING,
        description=(
            "Revise food fields or restore status=active; arrays replace fully. "
            "Requires observed revision and reason. Preserves old revisions and "
            "unique identities; does not change past meals."
        ),
    )
    def nutrition_update_food(
        food_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        changes: dict[str, Any],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_update_food",
            ctx,
            inventory.update_food,
            food_id,
            expected_revision,
            reason,
            changes,
        )

    @server.tool(
        annotations=MUTATING,
        description=(
            "Archive a food without releasing its identity keys or changing "
            "historical references. Restore using update_food."
        ),
    )
    def nutrition_archive_food(
        food_id: str,
        expected_revision: Annotated[int, Field(ge=1)],
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_archive_food",
            ctx,
            inventory.update_food,
            food_id,
            expected_revision,
            reason,
            {"status": "archived"},
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Preview server-calculated nutrition for a pinned food and measured "
            "quantity, fixed serving fraction, or variable pack with supplied "
            "weight."
        ),
    )
    def nutrition_resolve_food(
        food_id: str,
        food_revision: Annotated[int, Field(ge=1)],
        amount: FoodAmount,
        ctx: MCPContext,  # type: ignore[type-arg]
        portion_estimation: Estimation | None = None,
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_resolve_food",
            ctx,
            inventory.resolve_food,
            food_id,
            food_revision,
            amount,
            portion_estimation,
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Page historical components with original values and candidate catalog "
            "identities for individual review. Names alone do not prove identity. "
            "Optional food_id supplies a comparison candidate; no writes."
        ),
    )
    def nutrition_find_food_matches(
        window: QueryWindow,
        ctx: MCPContext,  # type: ignore[type-arg]
        food_id: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
        unlinked_only: bool = True,
    ) -> dict[str, Any]:
        if "timezone" not in window.model_fields_set:
            window = window.model_copy(update={"timezone": default_timezone})
        return invoke(
            "nutrition_find_food_matches",
            ctx,
            inventory.find_food_matches,
            window,
            food_id,
            query,
            cursor,
            limit,
            unlinked_only,
        )

    @server.tool(
        annotations=MUTATING,
        description=(
            "Persist a 24-hour plan linking individually reviewed historical "
            "components to pinned food identities. Preserve original "
            "nutrition/provenance, show differences; no meal changes yet."
        ),
    )
    def nutrition_preview_food_links(
        links: Annotated[list[FoodLink], Field(min_length=1, max_length=100)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke("nutrition_preview_food_links", ctx, inventory.preview_food_links, links)

    @server.tool(
        annotations=MUTATING,
        description=(
            "Apply precisely a reviewed link plan atomically, rejecting stale "
            "entries. Preserves nutrient values and completeness; records audit "
            "revisions. Applied-plan retries return the stored result."
        ),
    )
    def nutrition_apply_food_links(
        plan_id: str,
        reason: Annotated[str, Field(min_length=1, max_length=500)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_apply_food_links", ctx, inventory.apply_food_links, plan_id, reason
        )

    external = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )

    @server.tool(
        annotations=external,
        description=(
            "Search USDA before estimating a new food. Returns candidates and a "
            "lookup receipt, including explicit unavailable outcomes; sends only "
            "food query terms to USDA."
        ),
    )
    def nutrition_search_usda_foods(
        query: Annotated[str, Field(min_length=1, max_length=300)],
        ctx: MCPContext,  # type: ignore[type-arg]
        data_types: list[Literal["Foundation", "SR Legacy", "Survey (FNDDS)", "Branded"]]
        | None = None,
        page: Annotated[int, Field(ge=1, le=1000)] = 1,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> dict[str, Any]:
        return invoke(
            "nutrition_search_usda_foods", ctx, usda.search, query, data_types, page, limit
        )

    @server.tool(
        annotations=external,
        description=(
            "Retrieve a USDA food with normalized per-100-g nutrients, original "
            "evidence, and immutable source_snapshot_id. Use its FDC ID once in the"
            " catalog; proxies reference that food."
        ),
    )
    def nutrition_get_usda_food(
        fdc_id: Annotated[int, Field(gt=0)],
        ctx: MCPContext,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        return invoke("nutrition_get_usda_food", ctx, usda.get_food, fdc_id)
