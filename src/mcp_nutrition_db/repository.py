"""Transactional SQLite persistence for nutrition entries and goals."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    DEFAULT_TIMEZONE,
    EntryChanges,
    GoalInput,
    ListEntriesInput,
    LogEntryInput,
    NutritionValues,
    SummarizeInput,
    resolve_window,
    validate_timezone,
)

NUTRIENTS: dict[str, tuple[str, int]] = {
    "calories_kcal": ("calories_mkcal", 1_000),
    "protein_g": ("protein_mg", 1_000),
    "carbohydrate_g": ("carbohydrate_mg", 1_000),
    "fat_g": ("fat_mg", 1_000),
    "fiber_g": ("fiber_mg", 1_000),
    "sugar_g": ("sugar_mg", 1_000),
    "sodium_mg": ("sodium_mg", 1),
}

SCHEMA_VERSION = 1
CREATE_RETRY_WINDOW = timedelta(minutes=10)


class RepositoryError(RuntimeError):
    """Base class for expected persistence failures."""


class NotFoundError(RepositoryError):
    pass


class RevisionConflictError(RepositoryError):
    def __init__(self, entry_id: str, expected: int, current: int) -> None:
        super().__init__(
            f"revision conflict for entry {entry_id}: expected {expected}, current {current}"
        )
        self.entry_id = entry_id
        self.expected = expected
        self.current = current


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _scale(value: float | None, factor: int) -> int | None:
    if value is None:
        return None
    return int((Decimal(str(value)) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _unscale(value: int | None, factor: int) -> float | None:
    if value is None:
        return None
    return float(Decimal(value) / factor)


def _nutrient_db_values(nutrition: NutritionValues) -> dict[str, int | None]:
    return {
        column: _scale(getattr(nutrition, public_name), factor)
        for public_name, (column, factor) in NUTRIENTS.items()
    }


def _nutrient_public_values(
    row: sqlite3.Row | Mapping[str, Any],
) -> dict[str, float | None]:
    return {
        public_name: _unscale(row[column], factor)
        for public_name, (column, factor) in NUTRIENTS.items()
    }


def _new_id() -> str:
    return str(uuid.uuid4())


MIGRATION_1 = """
CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    occurred_at TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT,
    estimation_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE entry_components (
    component_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    quantity TEXT,
    unit TEXT,
    portion_notes TEXT,
    source_type TEXT NOT NULL,
    source_detail TEXT,
    calories_mkcal INTEGER,
    protein_mg INTEGER,
    carbohydrate_mg INTEGER,
    fat_mg INTEGER,
    fiber_mg INTEGER,
    sugar_mg INTEGER,
    sodium_mg INTEGER,
    UNIQUE(entry_id, position)
);

CREATE TABLE entry_revisions (
    revision_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id),
    resulting_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE daily_goals (
    goal_id TEXT PRIMARY KEY,
    effective_from TEXT NOT NULL,
    timezone TEXT NOT NULL,
    calories_mkcal INTEGER,
    protein_mg INTEGER,
    carbohydrate_mg INTEGER,
    fat_mg INTEGER,
    fiber_mg INTEGER,
    sugar_mg INTEGER,
    sodium_mg INTEGER,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(effective_from, timezone)
);

CREATE TABLE goal_revisions (
    revision_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE create_fingerprints (
    request_digest TEXT NOT NULL,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX entries_active_time_idx
    ON entries(occurred_at_utc DESC, entry_id DESC) WHERE deleted_at IS NULL;
CREATE INDEX entries_kind_time_idx
    ON entries(kind, occurred_at_utc DESC) WHERE deleted_at IS NULL;
CREATE INDEX entry_components_entry_idx ON entry_components(entry_id, position);
CREATE INDEX daily_goals_effective_idx ON daily_goals(timezone, effective_from DESC);
CREATE INDEX create_fingerprints_digest_idx
    ON create_fingerprints(request_digest, created_at DESC);
"""


class NutritionRepository:
    def __init__(self, database_path: str | Path, *, clock: Any = _utc_now) -> None:
        self.database_path = str(database_path)
        self.clock = clock
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if self.database_path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            versions = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in versions:
                connection.executescript(MIGRATION_1)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _timestamp(self.clock())),
                )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"] or 0)

    @staticmethod
    def _create_digest(request: LogEntryInput) -> str:
        payload = request.model_dump(mode="json", exclude={"force_new"})
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode()).hexdigest()

    @staticmethod
    def _insert_components(
        connection: sqlite3.Connection, entry_id: str, components: Iterable[Any]
    ) -> None:
        for position, component in enumerate(components):
            nutrition = _nutrient_db_values(component.nutrition)
            connection.execute(
                """
                INSERT INTO entry_components(
                    component_id, entry_id, position, name, quantity, unit,
                    portion_notes, source_type, source_detail, calories_mkcal,
                    protein_mg, carbohydrate_mg, fat_mg, fiber_mg, sugar_mg,
                    sodium_mg
                ) VALUES (
                    :component_id, :entry_id, :position, :name, :quantity, :unit,
                    :portion_notes, :source_type, :source_detail, :calories_mkcal,
                    :protein_mg, :carbohydrate_mg, :fat_mg, :fiber_mg, :sugar_mg,
                    :sodium_mg
                )
                """,
                {
                    "component_id": _new_id(),
                    "entry_id": entry_id,
                    "position": position,
                    "name": component.name,
                    "quantity": None
                    if component.quantity is None
                    else str(Decimal(str(component.quantity))),
                    "unit": component.unit,
                    "portion_notes": component.portion_notes,
                    "source_type": component.source.type.value,
                    "source_detail": component.source.detail,
                    **nutrition,
                },
            )

    def create_entry(self, request: LogEntryInput) -> dict[str, Any]:
        now = self.clock()
        now_text = _timestamp(now)
        digest = self._create_digest(request)
        cutoff = _timestamp(now - CREATE_RETRY_WINDOW)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM create_fingerprints WHERE created_at < ?", (cutoff,))
            if not request.force_new:
                prior = connection.execute(
                    """
                    SELECT f.entry_id
                    FROM create_fingerprints AS f
                    JOIN entries AS e ON e.entry_id = f.entry_id
                    WHERE f.request_digest = ? AND f.created_at >= ?
                      AND e.deleted_at IS NULL
                    ORDER BY f.created_at DESC
                    LIMIT 1
                    """,
                    (digest, cutoff),
                ).fetchone()
                if prior is not None:
                    entry = self._get_entry(connection, prior["entry_id"])
                    connection.commit()
                    return {**entry, "deduplicated": True}

            entry_id = _new_id()
            estimation_json = (
                None if request.estimation is None else request.estimation.model_dump_json()
            )
            connection.execute(
                """
                INSERT INTO entries(
                    entry_id, revision, occurred_at, occurred_at_utc, timezone,
                    kind, title, notes, estimation_json, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    request.occurred_at.isoformat(),
                    _timestamp(request.occurred_at),
                    request.timezone,
                    request.kind.value,
                    request.title,
                    request.notes,
                    estimation_json,
                    now_text,
                    now_text,
                ),
            )
            self._insert_components(connection, entry_id, request.components)
            if not request.force_new:
                connection.execute(
                    "INSERT INTO create_fingerprints(request_digest, entry_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (digest, entry_id, now_text),
                )
            entry = self._get_entry(connection, entry_id)
            connection.commit()
            return {**entry, "deduplicated": False}

    @staticmethod
    def _component_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "component_id": row["component_id"],
            "name": row["name"],
            "quantity": None if row["quantity"] is None else float(Decimal(row["quantity"])),
            "unit": row["unit"],
            "portion_notes": row["portion_notes"],
            "source": {"type": row["source_type"], "detail": row["source_detail"]},
            "nutrition": _nutrient_public_values(row),
        }

    @staticmethod
    def _totals(components: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        totals: dict[str, float | None] = {}
        completeness: dict[str, dict[str, int | bool]] = {}
        total_components = len(components)
        for nutrient in NUTRIENTS:
            known = [
                component["nutrition"][nutrient]
                for component in components
                if component["nutrition"][nutrient] is not None
            ]
            totals[nutrient] = None if not known else round(sum(known), 3)
            completeness[nutrient] = {
                "known_components": len(known),
                "total_components": total_components,
                "complete": len(known) == total_components,
            }
        return totals, completeness

    def _get_entry(
        self, connection: sqlite3.Connection, entry_id: str, *, include_deleted: bool = False
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
        components = [self._component_from_row(component) for component in component_rows]
        totals, completeness = self._totals(components)
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

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._get_entry(connection, entry_id)

    def update_entry(
        self,
        entry_id: str,
        expected_revision: int,
        reason: str,
        changes: EntryChanges,
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_entry(connection, entry_id)
            if current["revision"] != expected_revision:
                raise RevisionConflictError(entry_id, expected_revision, current["revision"])

            fields = changes.model_fields_set
            values: dict[str, Any] = {}
            if "occurred_at" in fields:
                assert changes.occurred_at is not None
                values["occurred_at"] = changes.occurred_at.isoformat()
                values["occurred_at_utc"] = _timestamp(changes.occurred_at)
            if "timezone" in fields:
                assert changes.timezone is not None
                values["timezone"] = changes.timezone
            if "kind" in fields:
                assert changes.kind is not None
                values["kind"] = changes.kind.value
            if "title" in fields:
                assert changes.title is not None
                values["title"] = changes.title
            if "notes" in fields:
                values["notes"] = changes.notes
            if "estimation" in fields:
                values["estimation_json"] = (
                    None if changes.estimation is None else changes.estimation.model_dump_json()
                )

            new_revision = expected_revision + 1
            values["revision"] = new_revision
            values["updated_at"] = now_text
            assignments = ", ".join(f"{column} = :{column}" for column in values)
            connection.execute(
                f"UPDATE entries SET {assignments} WHERE entry_id = :entry_id",
                {**values, "entry_id": entry_id},
            )
            if "components" in fields:
                assert changes.components is not None
                connection.execute("DELETE FROM entry_components WHERE entry_id = ?", (entry_id,))
                self._insert_components(connection, entry_id, changes.components)

            connection.execute(
                """
                INSERT INTO entry_revisions(
                    revision_id, entry_id, resulting_revision, operation, reason,
                    snapshot_json, created_at
                ) VALUES (?, ?, ?, 'update', ?, ?, ?)
                """,
                (
                    _new_id(),
                    entry_id,
                    new_revision,
                    reason,
                    json.dumps(current, sort_keys=True),
                    now_text,
                ),
            )
            updated = self._get_entry(connection, entry_id)
            connection.commit()
            return updated

    def delete_entry(self, entry_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_entry(connection, entry_id)
            if current["revision"] != expected_revision:
                raise RevisionConflictError(entry_id, expected_revision, current["revision"])
            new_revision = expected_revision + 1
            connection.execute(
                """
                UPDATE entries SET revision = ?, updated_at = ?, deleted_at = ?
                WHERE entry_id = ?
                """,
                (new_revision, now_text, now_text, entry_id),
            )
            connection.execute(
                """
                INSERT INTO entry_revisions(
                    revision_id, entry_id, resulting_revision, operation, reason,
                    snapshot_json, created_at
                ) VALUES (?, ?, ?, 'delete', ?, ?, ?)
                """,
                (
                    _new_id(),
                    entry_id,
                    new_revision,
                    reason,
                    json.dumps(current, sort_keys=True),
                    now_text,
                ),
            )
            connection.commit()
        return {"entry_id": entry_id, "revision": new_revision, "deleted": True}

    @staticmethod
    def _encode_cursor(occurred_at_utc: str, entry_id: str) -> str:
        raw = json.dumps([occurred_at_utc, entry_id], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            values = json.loads(base64.urlsafe_b64decode(padded).decode())
            if not isinstance(values, list) or len(values) != 2:
                raise ValueError
            return str(values[0]), str(values[1])
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid pagination cursor") from error

    def list_entries(
        self, request: ListEntriesInput, *, now: datetime | None = None
    ) -> dict[str, Any]:
        resolved = resolve_window(request.window, now=now or self.clock())
        parameters: list[Any] = [_timestamp(resolved.start), _timestamp(resolved.end)]
        clauses = [
            "deleted_at IS NULL",
            "occurred_at_utc >= ?",
            "occurred_at_utc < ?",
        ]
        if request.kind is not None:
            clauses.append("kind = ?")
            parameters.append(request.kind.value)
        if request.cursor is not None:
            cursor_time, cursor_id = self._decode_cursor(request.cursor)
            clauses.append("(occurred_at_utc < ? OR (occurred_at_utc = ? AND entry_id < ?))")
            parameters.extend([cursor_time, cursor_time, cursor_id])
        parameters.append(request.limit + 1)
        sql = (
            "SELECT entry_id, occurred_at_utc FROM entries WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at_utc DESC, entry_id DESC LIMIT ?"
        )
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            has_more = len(rows) > request.limit
            page = rows[: request.limit]
            entries = []
            for row in page:
                entry = self._get_entry(connection, row["entry_id"])
                entry.pop("components")
                entries.append(entry)
        next_cursor = None
        if has_more and page:
            next_cursor = self._encode_cursor(page[-1]["occurred_at_utc"], page[-1]["entry_id"])
        return {
            "entries": entries,
            "next_cursor": next_cursor,
            "resolved_window": resolved.model_dump(mode="json"),
        }

    def summarize(self, request: SummarizeInput, *, now: datetime | None = None) -> dict[str, Any]:
        resolved = resolve_window(request.window, now=now or self.clock())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT entry_id FROM entries
                WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
                ORDER BY occurred_at_utc
                """,
                (_timestamp(resolved.start), _timestamp(resolved.end)),
            ).fetchall()
            entries = [self._get_entry(connection, row["entry_id"]) for row in rows]

        zone = ZoneInfo(resolved.timezone)
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            key = "whole_range"
            if request.grouping == "day":
                key = (
                    _parse_timestamp(
                        datetime.fromisoformat(entry["occurred_at"]).astimezone(UTC).isoformat()
                    )
                    .astimezone(zone)
                    .date()
                    .isoformat()
                )
            groups.setdefault(key, []).append(entry)
        if not groups and request.grouping == "whole_range":
            groups["whole_range"] = []

        summaries = []
        for key, group_entries in groups.items():
            values: dict[str, float | None] = {}
            completeness: dict[str, dict[str, int | bool]] = {}
            for nutrient in NUTRIENTS:
                known = [
                    entry["totals"][nutrient]
                    for entry in group_entries
                    if entry["totals"][nutrient] is not None
                ]
                values[nutrient] = None if not known else round(sum(known), 3)
                completeness[nutrient] = {
                    "known_entries": len(known),
                    "total_entries": len(group_entries),
                    "complete": len(known) == len(group_entries),
                }
            goal = None
            goal_progress = None
            if request.grouping == "day":
                goal = self.get_goals(
                    on_date=date.fromisoformat(key),
                    timezone=resolved.timezone,
                    include_history=False,
                )["current"]
                if goal is not None:
                    goal_progress = {}
                    for nutrient, target in goal["targets"].items():
                        consumed = values[nutrient]
                        if target is None:
                            continue
                        goal_progress[nutrient] = {
                            "target": target,
                            "consumed": consumed,
                            "remaining": None if consumed is None else round(target - consumed, 3),
                            "fraction": None if consumed is None else round(consumed / target, 6),
                        }
            summaries.append(
                {
                    "group": key,
                    "entry_count": len(group_entries),
                    "totals": values,
                    "completeness": completeness,
                    "goal": goal,
                    "goal_progress": goal_progress,
                }
            )
        return {
            "grouping": request.grouping,
            "groups": summaries,
            "resolved_window": resolved.model_dump(mode="json"),
        }

    @staticmethod
    def _goal_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "goal_id": row["goal_id"],
            "effective_from": row["effective_from"],
            "timezone": row["timezone"],
            "targets": _nutrient_public_values(row),
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_goals(self, request: GoalInput) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        nutrients = _nutrient_db_values(request.targets)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM daily_goals WHERE effective_from = ? AND timezone = ?",
                (request.effective_from.isoformat(), request.timezone),
            ).fetchone()
            if existing is None:
                goal_id = _new_id()
                connection.execute(
                    """
                    INSERT INTO daily_goals(
                        goal_id, effective_from, timezone, calories_mkcal,
                        protein_mg, carbohydrate_mg, fat_mg, fiber_mg, sugar_mg,
                        sodium_mg, reason, created_at, updated_at
                    ) VALUES (
                        :goal_id, :effective_from, :timezone, :calories_mkcal,
                        :protein_mg, :carbohydrate_mg, :fat_mg, :fiber_mg,
                        :sugar_mg, :sodium_mg, :reason, :created_at, :updated_at
                    )
                    """,
                    {
                        "goal_id": goal_id,
                        "effective_from": request.effective_from.isoformat(),
                        "timezone": request.timezone,
                        "reason": request.reason,
                        "created_at": now_text,
                        "updated_at": now_text,
                        **nutrients,
                    },
                )
            else:
                goal_id = existing["goal_id"]
                connection.execute(
                    """
                    INSERT INTO goal_revisions(
                        revision_id, goal_id, reason, snapshot_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id(),
                        goal_id,
                        request.reason,
                        json.dumps(self._goal_from_row(existing), sort_keys=True),
                        now_text,
                    ),
                )
                connection.execute(
                    """
                    UPDATE daily_goals SET
                        calories_mkcal = :calories_mkcal,
                        protein_mg = :protein_mg,
                        carbohydrate_mg = :carbohydrate_mg,
                        fat_mg = :fat_mg,
                        fiber_mg = :fiber_mg,
                        sugar_mg = :sugar_mg,
                        sodium_mg = :sodium_mg,
                        reason = :reason,
                        updated_at = :updated_at
                    WHERE goal_id = :goal_id
                    """,
                    {
                        "goal_id": goal_id,
                        "reason": request.reason,
                        "updated_at": now_text,
                        **nutrients,
                    },
                )
            row = connection.execute(
                "SELECT * FROM daily_goals WHERE goal_id = ?", (goal_id,)
            ).fetchone()
            assert row is not None
            result = self._goal_from_row(row)
            connection.commit()
            return result

    def get_goals(
        self,
        *,
        on_date: date | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        include_history: bool = True,
    ) -> dict[str, Any]:
        validate_timezone(timezone)
        effective_date = on_date or self.clock().astimezone(ZoneInfo(timezone)).date()
        with self._connect() as connection:
            current = connection.execute(
                """
                SELECT * FROM daily_goals
                WHERE timezone = ? AND effective_from <= ?
                ORDER BY effective_from DESC LIMIT 1
                """,
                (timezone, effective_date.isoformat()),
            ).fetchone()
            history_rows: list[sqlite3.Row] = []
            if include_history:
                history_rows = connection.execute(
                    "SELECT * FROM daily_goals WHERE timezone = ? ORDER BY effective_from DESC",
                    (timezone,),
                ).fetchall()
        return {
            "on_date": effective_date.isoformat(),
            "timezone": timezone,
            "current": None if current is None else self._goal_from_row(current),
            "history": [self._goal_from_row(row) for row in history_rows],
        }

    def revision_history(self, entry_id: str) -> list[dict[str, Any]]:
        """Internal helper used by tests and future operator tooling."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resulting_revision, operation, reason, snapshot_json, created_at
                FROM entry_revisions WHERE entry_id = ? ORDER BY resulting_revision
                """,
                (entry_id,),
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
