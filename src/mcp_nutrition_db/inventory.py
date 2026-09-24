"""Versioned catalog, deterministic identity constraints, and MCP-driven history links."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import unicodedata
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from .inventory_models import FixedServing, FoodDefinition, FoodLink, SourceEvidence
from .models import (
    Estimation,
    FoodAmount,
    NutritionValues,
    QuantityAmount,
    QueryWindow,
    ServingAmount,
    resolve_window,
)
from .repository import (
    NUTRIENTS,
    RepositoryError,
    _new_id,
    _nutrient_db_values,
    _parse_timestamp,
    _timestamp,
)

if TYPE_CHECKING:
    from .repository import NutritionRepository


class InventoryError(RepositoryError):
    def __init__(self, code: str, **details: Any) -> None:
        self.code = code
        self.details = details
        super().__init__(json.dumps({"code": code, **details}, ensure_ascii=False))


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _cursor(filters: Any, position: str) -> str:
    return base64.urlsafe_b64encode(
        _json([hashlib.sha256(_json(filters).encode()).hexdigest(), position]).encode()
    ).decode()


def _position(cursor: str | None, filters: Any) -> str:
    if cursor is None:
        return ""
    try:
        digest, position = json.loads(base64.urlsafe_b64decode(cursor))
        if digest != hashlib.sha256(_json(filters).encode()).hexdigest():
            raise ValueError("cursor does not match filters")
        if not isinstance(position, str):
            raise ValueError("invalid position")
        return position
    except (ValueError, TypeError, UnicodeError) as error:
        raise ValueError("invalid inventory cursor") from error


class InventoryRepository:
    def __init__(self, repository: NutritionRepository) -> None:
        self.repo = repository

    @staticmethod
    def _keys(food: FoodDefinition) -> list[tuple[str, str]]:
        keys = [("short_name", normalize_name(food.short_name))]
        if food.usda_fdc_id is not None:
            keys.append(("usda_fdc_id", str(food.usda_fdc_id)))
        for identifier in food.identifiers:
            value = (
                identifier.value.zfill(14)
                if identifier.scheme == "gtin"
                else _json([normalize_name(identifier.vendor or ""), identifier.value])
            )
            keys.append((identifier.scheme, value))
        return sorted(set(keys))

    def _claim_keys(
        self, connection: sqlite3.Connection, food_id: str, food: FoodDefinition
    ) -> None:
        conflicts = []
        for kind, value in self._keys(food):
            row = connection.execute(
                "SELECT f.* FROM food_identity_keys k JOIN foods f USING(food_id) "
                "WHERE k.kind=? AND k.value=? AND k.food_id != ?",
                (kind, value, food_id),
            ).fetchone()
            if row:
                conflicts.append(
                    {
                        "key": kind,
                        "value": value,
                        "food_id": row["food_id"],
                        "short_name": json.loads(row["definition_json"])["short_name"],
                        "revision": row["revision"],
                        "status": row["status"],
                    }
                )
        if conflicts:
            raise InventoryError("food_identity_conflict", conflicts=conflicts)
        for kind, value in self._keys(food):
            connection.execute(
                "INSERT OR IGNORE INTO food_identity_keys VALUES (?, ?, ?)",
                (kind, value, food_id),
            )

    @staticmethod
    def _food(
        connection: sqlite3.Connection, food_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM foods WHERE food_id=?", (food_id,)).fetchone()
        if row is None:
            raise InventoryError("food_not_found", food_id=food_id)
        if revision is not None:
            version = connection.execute(
                "SELECT snapshot_json FROM food_revisions WHERE food_id=? AND revision=?",
                (food_id, revision),
            ).fetchone()
            if version is None:
                raise InventoryError("food_revision_not_found", food_id=food_id, revision=revision)
            result: dict[str, Any] = json.loads(version["snapshot_json"])
            return {**result, "current_status": row["status"]}
        return {
            **json.loads(row["definition_json"]),
            "food_id": food_id,
            "revision": row["revision"],
            "status": row["status"],
            "current_status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_food(self, food_id: str, revision: int | None = None) -> dict[str, Any]:
        with self.repo._connect() as connection:
            if revision is None:
                revision = self._food(connection, food_id)["revision"]
            return self._food(connection, food_id, revision)

    def status(self) -> dict[str, Any]:
        with self.repo._connect() as connection:
            entries = connection.execute(
                "SELECT COUNT(*) AS entries, MIN(occurred_at_utc) AS first_occurred_at, "
                "MAX(occurred_at_utc) AS last_occurred_at FROM entries WHERE deleted_at IS NULL"
            ).fetchone()
            components = connection.execute(
                "SELECT COUNT(*) AS components, COALESCE(SUM(c.food_id IS NOT NULL), 0) "
                "AS linked_components, COALESCE(SUM(c.food_id IS NULL), 0) AS unlinked_components "
                "FROM entry_components c JOIN entries e USING(entry_id) WHERE e.deleted_at IS NULL"
            ).fetchone()
            foods = connection.execute(
                "SELECT status, COUNT(*) AS count FROM foods GROUP BY status"
            ).fetchall()
            return {
                "history": {**dict(entries), **dict(components)},
                "foods": {row["status"]: row["count"] for row in foods},
                "schema_version": self.repo.schema_version(),
            }

    def _validate_source(
        self, connection: sqlite3.Connection, food: FoodDefinition
    ) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        sources: list[SourceEvidence] = [
            food.source,
            *(food.source.nutrient_sources or {}).values(),
        ]
        for index, source in enumerate(sources):
            if source.external_reference:
                ref = source.external_reference
                row = connection.execute(
                    "SELECT * FROM usda_snapshots WHERE source_snapshot_id=? AND fdc_id=?",
                    (ref.source_snapshot_id, ref.record_id),
                ).fetchone()
                if row is None:
                    raise ValueError("USDA reference must resolve to a retrieved snapshot")
                snapshot = json.loads(row["snapshot_json"])
                if food.basis.model_dump() != snapshot["basis"]:
                    raise ValueError("direct USDA food must use the retrieved nutrition basis")
                if food.nutrition.model_dump() != snapshot["nutrition"]:
                    raise ValueError("direct USDA values conflict with retrieved snapshot")
                evidence[str(index)] = snapshot
            if source.inventory_reference:
                ref_food = source.inventory_reference
                proxy = self._food(connection, ref_food.food_id, ref_food.food_revision)
                if proxy["usda_fdc_id"] is None:
                    raise ValueError("USDA proxy must reference a canonical USDA food")
                evidence[str(index)] = proxy
            if source.historical_reference:
                ref_history = source.historical_reference
                entry = self.repo._get_entry(connection, ref_history.entry_id, include_deleted=True)
                if entry["revision"] != ref_history.entry_revision:
                    row = connection.execute(
                        "SELECT snapshot_json FROM entry_revisions "
                        "WHERE entry_id=? AND resulting_revision=?",
                        (ref_history.entry_id, ref_history.entry_revision + 1),
                    ).fetchone()
                    if row is None:
                        raise ValueError("historical source revision not found")
                    entry = json.loads(row["snapshot_json"])
                component = next(
                    (
                        c
                        for c in entry["components"]
                        if c["component_id"] == ref_history.component_id
                    ),
                    None,
                )
                if component is None:
                    raise ValueError("historical source component not found")
                if source.type.value != component["source"]["type"]:
                    raise ValueError("historical source must preserve original source type")
                evidence[str(index)] = component
        if food.usda_lookup:
            lookup = food.usda_lookup
            receipts = []
            for lookup_id in lookup.lookup_ids:
                row = connection.execute(
                    "SELECT result_json FROM usda_lookups WHERE lookup_id=?", (lookup_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("unknown USDA lookup receipt")
                receipts.append(json.loads(row["result_json"]))
            from .usda import api_key

            if lookup.outcome == "no_suitable_match":
                if not receipts or any(r["outcome"] != "success" for r in receipts):
                    raise ValueError("no_suitable_match requires successful USDA search receipts")
            elif not any(r["outcome"] == "unavailable" for r in receipts) and api_key() is not None:
                raise ValueError("unavailable fallback requires failure receipt")
        return evidence

    def _write_food(
        self,
        connection: sqlite3.Connection,
        food_id: str,
        food: FoodDefinition,
        revision: int,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        evidence = self._validate_source(connection, food)
        now = _timestamp(self.repo.clock())
        if revision == 1:
            connection.execute(
                "INSERT INTO foods VALUES (?, ?, ?, ?, ?, ?)",
                (food_id, revision, status, food.model_dump_json(), now, now),
            )
        self._claim_keys(connection, food_id, food)
        connection.execute(
            "UPDATE foods SET revision=?, status=?, definition_json=?, updated_at=?"
            " WHERE food_id=?",
            (revision, status, food.model_dump_json(), now, food_id),
        )
        for term in [food.name, food.short_name, *food.aliases]:
            connection.execute(
                "INSERT OR IGNORE INTO food_search_terms VALUES (?, ?)",
                (normalize_name(term), food_id),
            )
        result = self._food(connection, food_id)
        result["source_evidence"] = evidence
        connection.execute(
            "INSERT INTO food_revisions VALUES (?, ?, ?, ?, ?)",
            (food_id, revision, _json(result), reason, now),
        )
        return result

    def create_food(self, food: FoodDefinition) -> dict[str, Any]:
        with self.repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._write_food(connection, _new_id(), food, 1, "active", "Created food")

    def update_food(
        self, food_id: str, expected_revision: int, reason: str, changes: dict[str, Any]
    ) -> dict[str, Any]:
        if not changes:
            raise ValueError("at least one changed field is required")
        with self.repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._food(connection, food_id)
            if current["revision"] != expected_revision:
                raise InventoryError(
                    "revision_conflict", food_id=food_id, current_revision=current["revision"]
                )
            status = changes.get("status", current["status"])
            if status not in ("active", "archived"):
                raise ValueError("invalid food status")
            definition = {k: current[k] for k in FoodDefinition.model_fields}
            food = FoodDefinition.model_validate(
                {**definition, **{k: v for k, v in changes.items() if k != "status"}}
            )
            if current["usda_fdc_id"] is not None and food.usda_fdc_id != current["usda_fdc_id"]:
                raise ValueError("USDA identities cannot be reassigned or removed")
            return self._write_food(
                connection, food_id, food, expected_revision + 1, status, reason
            )

    def search_foods(
        self,
        query: str | None = None,
        status: str = "active",
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        term = normalize_name(query or "")
        filters = [term, status]
        after = _position(cursor, filters)
        if status not in ("active", "archived", "all") or not 1 <= limit <= 100:
            raise ValueError("invalid search status or limit")
        with self.repo._connect() as connection:
            params: list[Any] = [after]
            clauses = ["f.food_id > ?"]
            if status != "all":
                clauses.append("f.status=?")
                params.append(status)
            if term:
                clauses.append(
                    "(EXISTS(SELECT 1 FROM food_identity_keys k WHERE "
                    "k.food_id=f.food_id AND k.value=?) OR EXISTS(SELECT 1 FROM "
                    "food_search_terms s WHERE s.food_id=f.food_id AND "
                    + " AND ".join("instr(s.term, ?) > 0" for _ in term.split())
                    + "))"
                )
                params.extend([term, *term.split()])
            rows = connection.execute(
                "SELECT f.* FROM foods f WHERE "
                + " AND ".join(clauses)
                + " ORDER BY f.food_id LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            foods = [self._food(connection, r["food_id"], r["revision"]) for r in rows[:limit]]
            for food in foods:
                food["match_reasons"] = ["catalog_query" if term else "catalog_listing"]
            return {
                "foods": foods,
                "next_cursor": _cursor(filters, rows[limit - 1]["food_id"])
                if len(rows) > limit
                else None,
            }

    def _resolve(
        self,
        connection: sqlite3.Connection,
        food_id: str,
        revision: int,
        amount: FoodAmount,
        portion_estimation: Estimation | None = None,
        *,
        allow_archived: bool = False,
    ) -> dict[str, Any]:
        snapshot = self._food(connection, food_id, revision)
        if snapshot["current_status"] != "active" and not allow_archived:
            raise InventoryError("food_archived", food_id=food_id)
        food = FoodDefinition.model_validate({k: snapshot[k] for k in FoodDefinition.model_fields})
        assumptions = []
        if isinstance(amount, QuantityAmount):
            quantity, unit = Decimal(str(amount.quantity)), amount.unit
        else:
            serving = next((s for s in food.servings if s.serving_key == amount.serving_key), None)
            if serving is None:
                raise InventoryError("invalid_serving", serving_key=amount.serving_key)
            if isinstance(amount, ServingAmount):
                if not isinstance(serving, FixedServing):
                    raise InventoryError(
                        "amount_required", detail="variable pack requires whole_amount"
                    )
                quantity = Decimal(str(serving.amount.quantity)) * Decimal(str(amount.count))
                unit = serving.amount.unit
                assumptions = serving.assumptions
            else:
                if isinstance(serving, FixedServing):
                    raise InventoryError(
                        "invalid_serving", detail="use quantity for measured fixed serving"
                    )
                quantity = Decimal(str(amount.whole_amount.quantity)) * Decimal(
                    str(amount.fraction)
                )
                unit = amount.whole_amount.unit
        if unit != food.basis.unit:
            raise InventoryError("incompatible_basis", required_unit=food.basis.unit)
        if not 0 < quantity <= 1_000_000:
            raise ValueError("resolved quantity out of range")
        multiplier = quantity / Decimal(str(food.basis.quantity))
        values = {}
        for nutrient, (_, scale) in NUTRIENTS.items():
            original = getattr(food.nutrition, nutrient)
            values[nutrient] = (
                None
                if original is None
                else float(
                    (Decimal(str(original)) * multiplier * scale).quantize(
                        Decimal(1), rounding=ROUND_HALF_UP
                    )
                    / scale
                )
            )
        nutrition = NutritionValues.model_validate(values)
        return {
            "name": food.name,
            "quantity": float(quantity),
            "unit": unit,
            "portion_notes": None,
            "source": {"type": food.source.type.value, "detail": food.source.detail},
            "source_evidence": {
                "source": food.source.model_dump(mode="json"),
                "estimation": snapshot["estimation"],
                "evidence": snapshot.get("source_evidence", {}),
            },
            "nutrition": nutrition.model_dump(),
            "inventory": {
                "food_id": food_id,
                "food_revision": revision,
                "amount": amount.model_dump(mode="json"),
                "resolved_amount": {"quantity": float(quantity), "unit": unit},
                "nutrition_mode": "calculated",
                "portion_estimation": None
                if portion_estimation is None
                else portion_estimation.model_dump(mode="json"),
                "serving_assumptions": assumptions,
            },
        }

    def resolve_food(
        self,
        food_id: str,
        food_revision: int,
        amount: FoodAmount,
        portion_estimation: Estimation | None = None,
    ) -> dict[str, Any]:
        with self.repo._connect() as connection:
            return self._resolve(connection, food_id, food_revision, amount, portion_estimation)

    def find_food_matches(
        self,
        window: QueryWindow,
        food_id: str | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
        unlinked_only: bool = True,
    ) -> dict[str, Any]:
        resolved = resolve_window(window, now=self.repo.clock())
        filters = [resolved.model_dump(mode="json"), food_id, query, unlinked_only]
        after = _position(cursor, filters)
        if not 1 <= limit <= 100:
            raise ValueError("invalid limit")
        with self.repo._connect() as connection:
            rows = connection.execute(
                "SELECT c.*, e.revision AS entry_revision, e.occurred_at, e.title "
                "FROM entry_components c JOIN entries e USING(entry_id) "
                "WHERE e.deleted_at IS NULL AND e.occurred_at_utc>=? AND e.occurred_at_utc<? "
                "AND c.component_id>? "
                + ("AND c.food_id IS NULL " if unlinked_only else "")
                + "ORDER BY c.component_id",
                (_timestamp(resolved.start), _timestamp(resolved.end), after),
            ).fetchall()
            if query:
                tokens = normalize_name(query).split()
                rows = [r for r in rows if all(t in normalize_name(r["name"]) for t in tokens)]
            matches = []
            for row in rows[:limit]:
                component = self.repo._component_from_row(row)
                if food_id:
                    candidates = [self._food(connection, food_id)]
                else:
                    candidate_rows = connection.execute(
                        "SELECT DISTINCT food_id FROM food_search_terms WHERE term=? LIMIT 10",
                        (normalize_name(row["name"]),),
                    ).fetchall()
                    candidates = [self._food(connection, r["food_id"]) for r in candidate_rows]
                matches.append(
                    {
                        "entry_id": row["entry_id"],
                        "entry_revision": row["entry_revision"],
                        "occurred_at": row["occurred_at"],
                        "title": row["title"],
                        "component": component,
                        "candidates": [
                            {
                                "food_id": f["food_id"],
                                "food_revision": f["revision"],
                                "short_name": f["short_name"],
                                "classification": "ambiguous",
                                "evidence": ["explicit_filter" if food_id else "normalized_name"],
                                "proposed_amount": None,
                            }
                            for f in candidates
                        ],
                        "suggested_short_name": normalize_name(row["name"]),
                    }
                )
            return {
                "matches": matches,
                "resolved_window": resolved.model_dump(mode="json"),
                "next_cursor": _cursor(filters, rows[limit - 1]["component_id"])
                if len(rows) > limit
                else None,
            }

    def preview_food_links(self, links: list[FoodLink]) -> dict[str, Any]:
        if not 1 <= len(links) <= 100:
            raise ValueError("link batch must contain 1-100 targets")
        targets = [link.component_id for link in links]
        if len(set(targets)) != len(targets):
            raise InventoryError("invalid_link_target", detail="duplicate target")
        with self.repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            results = []
            entries = {}
            for link in links:
                entry = self.repo._get_entry(connection, link.entry_id)
                if entry["revision"] != link.expected_entry_revision:
                    raise InventoryError(
                        "revision_conflict",
                        entry_id=link.entry_id,
                        current_revision=entry["revision"],
                    )
                component = next(
                    (c for c in entry["components"] if c["component_id"] == link.component_id), None
                )
                if component is None or component.get("inventory") is not None:
                    raise InventoryError("invalid_link_target", component_id=link.component_id)
                self._food(connection, link.food_id, link.food_revision)
                resolved = (
                    None
                    if link.amount is None
                    else self._resolve(
                        connection,
                        link.food_id,
                        link.food_revision,
                        link.amount,
                        allow_archived=True,
                    )
                )
                differences = {}
                if resolved:
                    before = _nutrient_db_values(
                        NutritionValues.model_validate(component["nutrition"])
                    )
                    after_values = _nutrient_db_values(
                        NutritionValues.model_validate(resolved["nutrition"])
                    )
                    for key, (column, _) in NUTRIENTS.items():
                        if before[column] != after_values[column]:
                            differences[key] = {
                                "historical": component["nutrition"][key],
                                "canonical": resolved["nutrition"][key],
                            }
                entries[link.entry_id] = entry["revision"]
                results.append(
                    {
                        "link": link.model_dump(mode="json"),
                        "before": component,
                        "inventory": {
                            "food_id": link.food_id,
                            "food_revision": link.food_revision,
                            "amount": None
                            if link.amount is None
                            else link.amount.model_dump(mode="json"),
                            "resolved_amount": None
                            if resolved is None
                            else resolved["inventory"]["resolved_amount"],
                            "nutrition_mode": "historical_snapshot",
                            "identity_evidence": link.identity_evidence,
                        },
                        "comparison": "unverifiable"
                        if resolved is None
                        else ("different" if differences else "equal"),
                        "differences": differences,
                    }
                )
            plan_id = _new_id()
            expires_at = _timestamp(self.repo.clock() + timedelta(hours=24))
            plan = {
                "plan_id": plan_id,
                "expires_at": expires_at,
                "links": results,
                "entry_revisions": entries,
                "entry_count": len(entries),
                "nutrition_totals_changed": False,
            }
            connection.execute(
                "INSERT INTO food_link_plans VALUES (?, ?, ?, NULL)",
                (plan_id, _json(plan), expires_at),
            )
            return plan

    def apply_food_links(self, plan_id: str, reason: str) -> dict[str, Any]:
        with self.repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM food_link_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise InventoryError("plan_not_found", plan_id=plan_id)
            if row["result_json"] is not None:
                result: dict[str, Any] = json.loads(row["result_json"])
                return result
            if _parse_timestamp(row["expires_at"]) <= self.repo.clock():
                raise InventoryError("plan_expired", plan_id=plan_id)
            plan = json.loads(row["plan_json"])
            before_entries = {}
            for entry_id, revision in plan["entry_revisions"].items():
                entry = self.repo._get_entry(connection, entry_id)
                if entry["revision"] != revision:
                    raise InventoryError(
                        "plan_conflict", entry_id=entry_id, current_revision=entry["revision"]
                    )
                before_entries[entry_id] = entry
            for target in plan["links"]:
                link = target["link"]
                result_cursor = connection.execute(
                    "UPDATE entry_components SET food_id=?, food_revision=?, inventory_json=? "
                    "WHERE component_id=? AND entry_id=? AND food_id IS NULL",
                    (
                        link["food_id"],
                        link["food_revision"],
                        _json(target["inventory"]),
                        link["component_id"],
                        link["entry_id"],
                    ),
                )
                if result_cursor.rowcount != 1:
                    raise InventoryError("plan_conflict", component_id=link["component_id"])
            now = _timestamp(self.repo.clock())
            affected = []
            for entry_id, entry in before_entries.items():
                revision = entry["revision"] + 1
                connection.execute(
                    "INSERT INTO entry_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_new_id(), entry_id, revision, "link_food", reason, _json(entry), now),
                )
                connection.execute(
                    "UPDATE entries SET revision=?, updated_at=? WHERE entry_id=?",
                    (revision, now, entry_id),
                )
                after = self.repo._get_entry(connection, entry_id)
                if (
                    after["totals"] != entry["totals"]
                    or after["completeness"] != entry["completeness"]
                ):
                    raise InventoryError("plan_conflict", detail="nutrition invariant violated")
                affected.append({"entry_id": entry_id, "revision": revision})
            result = {
                "plan_id": plan_id,
                "entries": affected,
                "linked_component_count": len(plan["links"]),
                "nutrition_totals_changed": False,
                "completeness_changed": False,
            }
            connection.execute(
                "UPDATE food_link_plans SET result_json=? WHERE plan_id=?", (_json(result), plan_id)
            )
            return result
