"""Transactional SQLite persistence for nutrition entries and goals."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database
from .energy import (
    CONFIDENCE_MULTIPLIERS_PERMILLE,
    POLICY_ID,
    credited_burn,
    energy_policy,
)
from .energy_repository import load_energy_balances
from .entries import load_entry, revision_history
from .errors import NotFoundError, RevisionConflictError
from .inventory import InventoryRepository
from .models import (
    DEFAULT_TIMEZONE,
    ActivityPlanInput,
    EntryChanges,
    GoalInput,
    InventoryComponentInput,
    ListEntriesInput,
    ListTrainingsInput,
    LogEntryInput,
    LogTrainingInput,
    NutritionValues,
    RetainComponentInput,
    SummarizeInput,
    TrainingChanges,
    resolve_window,
    validate_timezone,
)
from .nutrients import NUTRIENTS, aggregate_nutrition
from .nutrients import nutrient_db_values as _nutrient_db_values
from .nutrients import nutrient_public_values as _nutrient_public_values
from .nutrients import scale as _scale
from .nutrients import unscale as _unscale
from .serialization import create_digest, utc_now
from .serialization import new_id as _new_id
from .serialization import timestamp as _timestamp
from .training_details import add_garmin_details

CREATE_RETRY_WINDOW = timedelta(minutes=10)
ENERGY_POLICY_ID = POLICY_ID


class NutritionRepository:
    def __init__(
        self, database_path: str | Path, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.database = Database(database_path, clock=clock)
        self.migrate()
        self.inventory = InventoryRepository(self.database)

    @property
    def database_path(self) -> str:
        return self.database.path

    @property
    def clock(self) -> Callable[[], datetime]:
        return self.database.clock

    @clock.setter
    def clock(self, value: Callable[[], datetime]) -> None:
        self.database.clock = value

    def migrate(self) -> None:
        self.database.migrate()

    def schema_version(self) -> int:
        return self.database.schema_version()

    def _insert_components(
        self, connection: sqlite3.Connection, entry_id: str, components: Iterable[Any]
    ) -> None:
        for position, component in enumerate(components):
            if isinstance(component, InventoryComponentInput):
                data = self.inventory.resolve_in_transaction(
                    connection,
                    component.food_id,
                    component.food_revision,
                    component.amount,
                    component.portion_estimation,
                )
                data["portion_notes"] = component.portion_notes
            elif isinstance(component, dict):
                data = component
            else:
                data = component.model_dump(mode="json")
            nutrition = _nutrient_db_values(NutritionValues.model_validate(data["nutrition"]))
            inventory = data.get("inventory")
            connection.execute(
                """
                INSERT INTO entry_components(
                    component_id, entry_id, position, name, quantity, unit,
                    portion_notes, source_type, source_detail, calories_mkcal,
                    protein_mg, carbohydrate_mg, fat_mg, fiber_mg, sugar_mg,
                    sodium_mg, food_id, food_revision, inventory_json, source_evidence_json
                ) VALUES (
                    :component_id, :entry_id, :position, :name, :quantity, :unit,
                    :portion_notes, :source_type, :source_detail, :calories_mkcal,
                    :protein_mg, :carbohydrate_mg, :fat_mg, :fiber_mg, :sugar_mg,
                    :sodium_mg, :food_id, :food_revision, :inventory_json, :source_evidence_json
                )
                """,
                {
                    "component_id": data.get("component_id", _new_id()),
                    "entry_id": entry_id,
                    "position": position,
                    "name": data["name"],
                    "quantity": None
                    if data.get("quantity") is None
                    else str(Decimal(str(data["quantity"]))),
                    "unit": data.get("unit"),
                    "portion_notes": data.get("portion_notes"),
                    "source_type": data["source"]["type"],
                    "source_detail": data["source"].get("detail"),
                    "food_id": None if inventory is None else inventory["food_id"],
                    "food_revision": None if inventory is None else inventory["food_revision"],
                    "inventory_json": None if inventory is None else json.dumps(inventory),
                    "source_evidence_json": None
                    if data.get("source_evidence") is None
                    else json.dumps(data["source_evidence"]),
                    **nutrition,
                },
            )

    def create_entry(self, request: LogEntryInput) -> dict[str, Any]:
        now = self.clock()
        now_text = _timestamp(now)
        digest = create_digest(request)
        cutoff = _timestamp(now - CREATE_RETRY_WINDOW)

        with self.database.connection(write=True) as connection:
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
                    entry = load_entry(connection, prior["entry_id"])
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
            entry = load_entry(connection, entry_id)
            return {**entry, "deduplicated": False}

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            return load_entry(connection, entry_id)

    def update_entry(
        self,
        entry_id: str,
        expected_revision: int,
        reason: str,
        changes: EntryChanges,
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self.database.connection(write=True) as connection:
            current = load_entry(connection, entry_id)
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
                prior = {c["component_id"]: c for c in current["components"]}
                retained: set[str] = set()
                components: list[Any] = []
                for component in changes.components:
                    if isinstance(component, RetainComponentInput):
                        key = component.existing_component_id
                        if key not in prior or key in retained:
                            raise ValueError(
                                "retained component must belong to this entry and be unique"
                            )
                        retained.add(key)
                        components.append(prior[key])
                    else:
                        components.append(component)
                connection.execute("DELETE FROM entry_components WHERE entry_id = ?", (entry_id,))
                self._insert_components(connection, entry_id, components)

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
            updated = load_entry(connection, entry_id)
            return updated

    def delete_entry(self, entry_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self.database.connection(write=True) as connection:
            current = load_entry(connection, entry_id)
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
        return {"entry_id": entry_id, "revision": new_revision, "deleted": True}

    @staticmethod
    def _training_from_row(row: sqlite3.Row) -> dict[str, Any]:
        reported_burn_mkcal = int(row["calories_burned_mkcal"])
        multiplier = CONFIDENCE_MULTIPLIERS_PERMILLE[row["confidence"]]
        credited_burn_mkcal = credited_burn(reported_burn_mkcal, row["confidence"])
        return {
            "policy_id": ENERGY_POLICY_ID,
            "training_id": row["training_id"],
            "revision": row["revision"],
            "occurred_at": row["occurred_at"],
            "timezone": row["timezone"],
            "activity": row["activity"],
            "duration_minutes": _unscale(row["duration_milliseconds"], 60_000),
            "reported_burn_kcal": _unscale(reported_burn_mkcal, 1_000),
            "calorie_basis": "active",
            "credited_burn_kcal": _unscale(credited_burn_mkcal, 1_000),
            "confidence": row["confidence"],
            "confidence_multiplier": multiplier / 1_000,
            "measurement_method": row["measurement_method"],
            "source": {"type": row["source_type"], "detail": row["source_detail"]},
            "evidence": (
                None if row["evidence_json"] is None else json.loads(row["evidence_json"])
            ),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_training(
        self, connection: sqlite3.Connection, training_id: str, *, include_deleted: bool = False
    ) -> dict[str, Any]:
        query = "SELECT * FROM trainings WHERE training_id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        row = connection.execute(query, (training_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"training not found: {training_id}")
        return self._training_from_row(row)

    def create_training(self, request: LogTrainingInput) -> dict[str, Any]:
        with self.database.connection(write=True) as connection:
            result = self.create_training_in_transaction(connection, request)
        return result

    def create_training_in_transaction(
        self, connection: sqlite3.Connection, request: LogTrainingInput
    ) -> dict[str, Any]:
        now = self.clock()
        now_text = _timestamp(now)
        digest = create_digest(request)
        cutoff = _timestamp(now - CREATE_RETRY_WINDOW)
        connection.execute(
            "DELETE FROM training_create_fingerprints WHERE created_at < ?", (cutoff,)
        )
        if not request.force_new:
            prior = connection.execute(
                """
                SELECT f.training_id
                FROM training_create_fingerprints AS f
                JOIN trainings AS t ON t.training_id = f.training_id
                WHERE f.request_digest = ? AND f.created_at >= ?
                  AND t.deleted_at IS NULL
                ORDER BY f.created_at DESC LIMIT 1
                """,
                (digest, cutoff),
            ).fetchone()
            if prior is not None:
                training = self._get_training(connection, prior["training_id"])
                return {**training, "deduplicated": True}

        training_id = _new_id()
        connection.execute(
            """
            INSERT INTO trainings(
                training_id, revision, occurred_at, occurred_at_utc, timezone,
                activity, duration_milliseconds, calories_burned_mkcal,
                confidence, measurement_method, source_type, source_detail,
                evidence_json, notes, created_at, updated_at
            ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                training_id,
                request.occurred_at.isoformat(),
                _timestamp(request.occurred_at),
                request.timezone,
                request.activity,
                _scale(request.duration_minutes, 60_000),
                _scale(request.reported_burn_kcal, 1_000),
                request.confidence.value,
                request.measurement_method.value,
                request.source.type.value,
                request.source.detail,
                None if request.evidence is None else request.evidence.model_dump_json(),
                request.notes,
                now_text,
                now_text,
            ),
        )
        if not request.force_new:
            connection.execute(
                "INSERT INTO training_create_fingerprints"
                "(request_digest, training_id, created_at) VALUES (?, ?, ?)",
                (digest, training_id, now_text),
            )
        training = self._get_training(connection, training_id)
        return {**training, "deduplicated": False}

    def get_training(self, training_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            training = self._get_training(connection, training_id)
            add_garmin_details(connection, [training])
            return training

    def update_training(
        self,
        training_id: str,
        expected_revision: int,
        reason: str,
        changes: TrainingChanges,
    ) -> dict[str, Any]:
        with self.database.connection(write=True) as connection:
            result = self.update_training_in_transaction(
                connection, training_id, expected_revision, reason, changes
            )
        return result

    def update_training_in_transaction(
        self,
        connection: sqlite3.Connection,
        training_id: str,
        expected_revision: int,
        reason: str,
        changes: TrainingChanges,
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        current = self._get_training(connection, training_id)
        if current["revision"] != expected_revision:
            raise RevisionConflictError(
                training_id,
                expected_revision,
                current["revision"],
                record_type="training",
            )
        fields = changes.model_fields_set
        values: dict[str, Any] = {}
        if "occurred_at" in fields:
            assert changes.occurred_at is not None
            values["occurred_at"] = changes.occurred_at.isoformat()
            values["occurred_at_utc"] = _timestamp(changes.occurred_at)
        if "timezone" in fields:
            assert changes.timezone is not None
            values["timezone"] = changes.timezone
        if "activity" in fields:
            assert changes.activity is not None
            values["activity"] = changes.activity
        if "duration_minutes" in fields:
            assert changes.duration_minutes is not None
            values["duration_milliseconds"] = _scale(changes.duration_minutes, 60_000)
        if "reported_burn_kcal" in fields:
            assert changes.reported_burn_kcal is not None
            values["calories_burned_mkcal"] = _scale(changes.reported_burn_kcal, 1_000)
        if "confidence" in fields:
            assert changes.confidence is not None
            values["confidence"] = changes.confidence.value
        if "measurement_method" in fields:
            assert changes.measurement_method is not None
            values["measurement_method"] = changes.measurement_method.value
        if "source" in fields:
            assert changes.source is not None
            values["source_type"] = changes.source.type.value
            values["source_detail"] = changes.source.detail
        if "evidence" in fields:
            values["evidence_json"] = (
                None if changes.evidence is None else changes.evidence.model_dump_json()
            )
        if "notes" in fields:
            values["notes"] = changes.notes
        new_revision = expected_revision + 1
        values.update(revision=new_revision, updated_at=now_text)
        assignments = ", ".join(f"{column} = :{column}" for column in values)
        connection.execute(
            f"UPDATE trainings SET {assignments} WHERE training_id = :training_id",
            {**values, "training_id": training_id},
        )
        connection.execute(
            """
            INSERT INTO training_revisions(
                revision_id, training_id, resulting_revision, operation, reason,
                snapshot_json, created_at
            ) VALUES (?, ?, ?, 'update', ?, ?, ?)
            """,
            (
                _new_id(),
                training_id,
                new_revision,
                reason,
                json.dumps(current, sort_keys=True),
                now_text,
            ),
        )
        updated = self._get_training(connection, training_id)
        return updated

    def delete_training(
        self, training_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self.database.connection(write=True) as connection:
            current = self._get_training(connection, training_id)
            if current["revision"] != expected_revision:
                raise RevisionConflictError(
                    training_id,
                    expected_revision,
                    current["revision"],
                    record_type="training",
                )
            new_revision = expected_revision + 1
            connection.execute(
                "UPDATE trainings SET revision = ?, updated_at = ?, deleted_at = ? "
                "WHERE training_id = ?",
                (new_revision, now_text, now_text, training_id),
            )
            connection.execute(
                """
                INSERT INTO training_revisions(
                    revision_id, training_id, resulting_revision, operation, reason,
                    snapshot_json, created_at
                ) VALUES (?, ?, ?, 'delete', ?, ?, ?)
                """,
                (
                    _new_id(),
                    training_id,
                    new_revision,
                    reason,
                    json.dumps(current, sort_keys=True),
                    now_text,
                ),
            )
        return {"training_id": training_id, "revision": new_revision, "deleted": True}

    def list_trainings(
        self, request: ListTrainingsInput, *, now: datetime | None = None
    ) -> dict[str, Any]:
        resolved = resolve_window(request.window, now=now or self.clock())
        parameters: list[Any] = [_timestamp(resolved.start), _timestamp(resolved.end)]
        clauses = [
            "deleted_at IS NULL",
            "occurred_at_utc >= ?",
            "occurred_at_utc < ?",
        ]
        if request.cursor is not None:
            cursor_time, cursor_id = self._decode_cursor(request.cursor)
            clauses.append("(occurred_at_utc < ? OR (occurred_at_utc = ? AND training_id < ?))")
            parameters.extend([cursor_time, cursor_time, cursor_id])
        parameters.append(request.limit + 1)
        sql = (
            "SELECT * FROM trainings WHERE "
            + " AND ".join(clauses)
            + " ORDER BY occurred_at_utc DESC, training_id DESC LIMIT ?"
        )
        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            trainings = [self._training_from_row(row) for row in rows[: request.limit]]
            add_garmin_details(connection, trainings)
        has_more = len(rows) > request.limit
        page = rows[: request.limit]
        next_cursor = None
        if has_more and page:
            next_cursor = self._encode_cursor(page[-1]["occurred_at_utc"], page[-1]["training_id"])
        return {
            "trainings": trainings,
            "next_cursor": next_cursor,
            "resolved_window": resolved.model_dump(mode="json"),
        }

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
        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            has_more = len(rows) > request.limit
            page = rows[: request.limit]
            entries = []
            for row in page:
                entry = load_entry(connection, row["entry_id"])
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
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT entry_id FROM entries
                WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
                ORDER BY occurred_at_utc
                """,
                (_timestamp(resolved.start), _timestamp(resolved.end)),
            ).fetchall()
            entries = [load_entry(connection, row["entry_id"]) for row in rows]
            training_rows = connection.execute(
                """
                SELECT * FROM trainings
                WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
                ORDER BY occurred_at_utc
                """,
                (_timestamp(resolved.start), _timestamp(resolved.end)),
            ).fetchall()
            trainings = [self._training_from_row(row) for row in training_rows]
            add_garmin_details(connection, trainings)

        zone = ZoneInfo(resolved.timezone)
        groups: dict[str, list[dict[str, Any]]] = {}
        training_groups: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            key = "whole_range"
            if request.grouping == "day":
                key = (
                    datetime.fromisoformat(entry["occurred_at"]).astimezone(zone).date().isoformat()
                )
            groups.setdefault(key, []).append(entry)
        for training in trainings:
            key = "whole_range"
            if request.grouping == "day":
                key = (
                    datetime.fromisoformat(training["occurred_at"])
                    .astimezone(zone)
                    .date()
                    .isoformat()
                )
            training_groups.setdefault(key, []).append(training)
            groups.setdefault(key, [])
        if not groups and request.grouping == "whole_range":
            groups["whole_range"] = []

        summaries = []
        whole_range_goal_date = None
        if request.grouping == "whole_range":
            local_start = resolved.start.astimezone(zone)
            local_end = resolved.end.astimezone(zone)
            if (
                local_start.timetz().replace(tzinfo=None) == time.min
                and local_end.timetz().replace(tzinfo=None) == time.min
                and local_end.date() == local_start.date() + timedelta(days=1)
            ):
                whole_range_goal_date = local_start.date()
        summary_dates = (
            [date.fromisoformat(key) for key in groups]
            if request.grouping == "day"
            else ([] if whole_range_goal_date is None else [whole_range_goal_date])
        )
        balances: dict[str, dict[str, Any]] = {}
        goals_for_date: dict[date, dict[str, Any]] = {}
        if summary_dates:
            balances = load_energy_balances(
                self.database, min(summary_dates), max(summary_dates), resolved.timezone
            )
            with self.database.connection() as connection:
                summary_goal_rows = connection.execute(
                    """
                    SELECT * FROM daily_goals
                    WHERE timezone = ? AND effective_from <= ?
                    ORDER BY effective_from
                    """,
                    (resolved.timezone, max(summary_dates).isoformat()),
                ).fetchall()
            for summary_date in sorted(summary_dates):
                applicable = [
                    row
                    for row in summary_goal_rows
                    if date.fromisoformat(row["effective_from"]) <= summary_date
                ]
                if applicable:
                    goals_for_date[summary_date] = self._goal_from_row(applicable[-1])
        for key, group_entries in groups.items():
            group_trainings = training_groups.get(key, [])
            reported_training_burn = round(
                sum(training["reported_burn_kcal"] for training in group_trainings), 3
            )
            credited_training_burn = round(
                sum(training["credited_burn_kcal"] for training in group_trainings), 3
            )
            values, completeness = aggregate_nutrition(group_entries, level="entries")
            goal = None
            goal_progress = None
            goal_date = (
                date.fromisoformat(key) if request.grouping == "day" else whole_range_goal_date
            )
            if goal_date is not None:
                goal = goals_for_date.get(goal_date)
                if goal is not None:
                    goal["energy_budget"] = balances[goal_date.isoformat()]
                    goal_progress = {}
                    for nutrient, target in goal["targets"].items():
                        consumed = values[nutrient]
                        if target is None:
                            continue
                        goal_progress[nutrient] = {
                            "target": target,
                            "consumed": consumed,
                            "remaining": None if consumed is None else round(target - consumed, 3),
                            "fraction": (
                                None
                                if consumed is None or target == 0
                                else round(consumed / target, 6)
                            ),
                        }
                    if goal["energy_budget"] is not None:
                        energy = goal["energy_budget"]
                        calorie_consumed = energy["intake_kcal"]
                        goal_progress["calories_kcal"] = {
                            "consumed": calorie_consumed,
                            "intake_complete": energy["intake_complete"],
                            "ordinary_target": energy["ordinary_target_kcal"],
                            "remaining_to_ordinary_target": round(
                                energy["ordinary_target_kcal"] - calorie_consumed, 3
                            ),
                            "planned_baseline": energy["planned_baseline_kcal"],
                            "remaining_to_planned_baseline": round(
                                energy["planned_baseline_kcal"] - calorie_consumed, 3
                            ),
                            "available_ceiling": energy["available_ceiling_kcal"],
                            "remaining_to_available_ceiling": round(
                                energy["available_ceiling_kcal"] - calorie_consumed, 3
                            ),
                        }
            summaries.append(
                {
                    "group": key,
                    "entry_count": len(group_entries),
                    "training_count": len(group_trainings),
                    "trainings": group_trainings,
                    "reported_training_burn_kcal": reported_training_burn,
                    "credited_training_burn_kcal": credited_training_burn,
                    "totals": values,
                    "completeness": completeness,
                    "goal": goal,
                    "energy_balance": None if goal is None else goal["energy_budget"],
                    "goal_progress": goal_progress,
                }
            )
        return {
            "policy_id": ENERGY_POLICY_ID,
            "grouping": request.grouping,
            "groups": summaries,
            **self.conversation_status(resolved.timezone),
            "resolved_window": resolved.model_dump(mode="json"),
        }

    @staticmethod
    def _goal_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "goal_id": row["goal_id"],
            "effective_from": row["effective_from"],
            "timezone": row["timezone"],
            "targets": _nutrient_public_values(row),
            "base_burn_kcal": _unscale(row["base_burn_mkcal"], 1_000),
            "deficit_kcal": _unscale(row["deficit_mkcal"], 1_000),
            "reason": row["reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_goals(self, request: GoalInput) -> dict[str, Any]:
        with self.database.connection(write=True) as connection:
            result = self.set_goals_in_transaction(connection, request)
        result["energy_budget"] = self.energy_balance(request.effective_from, request.timezone)
        return result

    def set_goals_in_transaction(
        self, connection: sqlite3.Connection, request: GoalInput
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        nutrients = (
            {column: None for column, _factor in NUTRIENTS.values()}
            if request.targets is None
            else _nutrient_db_values(request.targets)
        )
        nutrients["calories_mkcal"] = None
        base_burn_mkcal = _scale(request.base_burn_kcal, 1_000)
        deficit_mkcal = _scale(request.deficit_kcal, 1_000)
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
                    sodium_mg, base_burn_mkcal, deficit_mkcal,
                    reason, created_at, updated_at
                ) VALUES (
                    :goal_id, :effective_from, :timezone, :calories_mkcal,
                    :protein_mg, :carbohydrate_mg, :fat_mg, :fiber_mg,
                    :sugar_mg, :sodium_mg, :base_burn_mkcal, :deficit_mkcal,
                    :reason, :created_at, :updated_at
                )
                """,
                {
                    "goal_id": goal_id,
                    "effective_from": request.effective_from.isoformat(),
                    "timezone": request.timezone,
                    "reason": request.reason,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "base_burn_mkcal": base_burn_mkcal,
                    "deficit_mkcal": deficit_mkcal,
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
                    base_burn_mkcal = :base_burn_mkcal,
                    deficit_mkcal = :deficit_mkcal,
                    reason = :reason,
                    updated_at = :updated_at
                WHERE goal_id = :goal_id
                """,
                {
                    "goal_id": goal_id,
                    "reason": request.reason,
                    "updated_at": now_text,
                    "base_burn_mkcal": base_burn_mkcal,
                    "deficit_mkcal": deficit_mkcal,
                    **nutrients,
                },
            )
        row = connection.execute(
            "SELECT * FROM daily_goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        assert row is not None
        result = self._goal_from_row(row)
        return result

    def _day_reviews(self, timezone: str) -> dict[str, dict[str, Any]]:
        validate_timezone(timezone)
        with self.database.connection() as connection:
            return {
                row["on_date"]: {
                    key: bool(value) if key == "exceptional_activity" else value
                    for key, value in dict(row).items()
                    if key != "intake_complete"
                }
                for row in connection.execute(
                    "SELECT * FROM day_reviews WHERE timezone = ?", (timezone,)
                )
            }

    def get_activity_plan(
        self, on_date: date | None = None, timezone: str = DEFAULT_TIMEZONE
    ) -> dict[str, Any]:
        validate_timezone(timezone)
        day = on_date or self.clock().astimezone(ZoneInfo(timezone)).date()
        reviews = self._day_reviews(timezone)
        return {
            "policy_id": ENERGY_POLICY_ID,
            "on_date": day.isoformat(),
            "timezone": timezone,
            "activity_plan": reviews.get(day.isoformat()),
        }

    def set_activity_plan(self, request: ActivityPlanInput) -> dict[str, Any]:
        table = "day_reviews"
        key = "on_date"
        record_type = "activity_plan"
        values = request.model_dump(mode="json", exclude={"expected_revision"})
        with self.database.connection(write=True) as connection:
            previous = connection.execute(
                f"SELECT * FROM {table} WHERE timezone = ? AND {key} = ?",
                (request.timezone, values[key]),
            ).fetchone()
            current_revision = 0 if previous is None else int(previous["revision"])
            if request.expected_revision != current_revision:
                raise RevisionConflictError(
                    values[key],
                    request.expected_revision,
                    current_revision,
                    record_type=record_type,
                )
            values["revision"] = current_revision + 1
            values["updated_at"] = _timestamp(self.clock())
            # Retain the deployed schema; the legacy completion flag is never read.
            stored_values = {**values, "intake_complete": 0}
            columns = list(stored_values)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(timezone, {key}) DO UPDATE SET "
                + ", ".join(f"{column} = excluded.{column}" for column in columns),
                tuple(stored_values.values()),
            )
            connection.execute(
                "INSERT INTO day_review_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_id(),
                    record_type,
                    request.timezone + ":" + values[key],
                    values["revision"],
                    request.reason,
                    json.dumps(values, sort_keys=True),
                    values["updated_at"],
                ),
            )
        return {"policy_id": ENERGY_POLICY_ID, "record": values}

    energy_policy = staticmethod(energy_policy)

    def energy_balance(self, on_date: date, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
        return load_energy_balances(self.database, on_date, on_date, timezone)[on_date.isoformat()]

    def conversation_status(self, timezone: str) -> dict[str, Any]:
        from .body import BodyRepository
        from .weight_review import WeightReviewRepository

        return {
            "weight_budget_review": WeightReviewRepository(self).status(timezone),
            "garmin_connection": BodyRepository(self.database).sync_status()["connection_hint"],
        }

    def get_goals(
        self,
        *,
        on_date: date | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        include_history: bool = True,
    ) -> dict[str, Any]:
        validate_timezone(timezone)
        effective_date = on_date or self.clock().astimezone(ZoneInfo(timezone)).date()
        with self.database.connection() as connection:
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
        current_goal = None if current is None else self._goal_from_row(current)
        if current_goal is not None:
            current_goal["energy_budget"] = self.energy_balance(effective_date, timezone)
        return {
            "policy_id": ENERGY_POLICY_ID,
            "on_date": effective_date.isoformat(),
            "timezone": timezone,
            "current": current_goal,
            "history": [self._goal_from_row(row) for row in history_rows],
            **self.conversation_status(timezone),
        }

    def revision_history(self, entry_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return revision_history(connection, "entry", entry_id)

    def training_revision_history(self, training_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return revision_history(connection, "training", training_id)
