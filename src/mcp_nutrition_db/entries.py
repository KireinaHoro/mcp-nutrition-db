"""Read canonical entry snapshots within a caller-owned transaction."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Literal

from .errors import NotFoundError
from .nutrients import aggregate_nutrition
from .nutrients import nutrient_public_values as _nutrient_public_values


def component_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "component_id": row["component_id"],
        "name": row["name"],
        "quantity": None if row["quantity"] is None else float(Decimal(row["quantity"])),
        "unit": row["unit"],
        "portion_notes": row["portion_notes"],
        "source": {"type": row["source_type"], "detail": row["source_detail"]},
        "nutrition": _nutrient_public_values(row),
        "inventory": None if row["inventory_json"] is None else json.loads(row["inventory_json"]),
        "source_evidence": None
        if row["source_evidence_json"] is None
        else json.loads(row["source_evidence_json"]),
    }


def load_entry(
    connection: sqlite3.Connection, entry_id: str, *, include_deleted: bool = False
) -> dict[str, Any]:
    query = "SELECT * FROM entries WHERE entry_id = ?"
    if not include_deleted:
        query += " AND deleted_at IS NULL"
    row = connection.execute(query, (entry_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"entry not found: {entry_id}")
    component_rows = connection.execute(
        "SELECT * FROM entry_components WHERE entry_id = ? ORDER BY position", (entry_id,)
    ).fetchall()
    components = [component_from_row(component) for component in component_rows]
    totals, completeness = aggregate_nutrition(components, level="components")
    return {
        "entry_id": row["entry_id"],
        "revision": row["revision"],
        "occurred_at": row["occurred_at"],
        "timezone": row["timezone"],
        "kind": row["kind"],
        "title": row["title"],
        "notes": row["notes"],
        "components": components,
        "totals": totals,
        "completeness": completeness,
        "estimation": None
        if row["estimation_json"] is None
        else json.loads(row["estimation_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def revision_history(
    connection: sqlite3.Connection, record_type: Literal["entry", "training"], record_id: str
) -> list[dict[str, Any]]:
    table, key = {
        "entry": ("entry_revisions", "entry_id"),
        "training": ("training_revisions", "training_id"),
    }[record_type]
    rows = connection.execute(
        "SELECT resulting_revision, operation, reason, snapshot_json, created_at "
        f"FROM {table} WHERE {key} = ? ORDER BY resulting_revision",
        (record_id,),
    ).fetchall()
    return [
        {
            "resulting_revision": row["resulting_revision"],
            "operation": row["operation"],
            "reason": row["reason"],
            "snapshot": json.loads(row["snapshot_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
