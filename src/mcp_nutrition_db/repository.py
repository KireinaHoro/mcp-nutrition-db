"""Transactional SQLite persistence for nutrition entries and goals."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .energy import (
    DAILY_ADJUSTMENT_MKCAL,
    DEBIT_EFFECTIVE_FROM,
    EXCEPTIONAL_BURN_MKCAL,
    EXCEPTIONAL_DURATION_MS,
    POLICY_ID,
    REVIEW_AFTER_ELIGIBLE_DAYS,
)
from .models import (
    DEFAULT_TIMEZONE,
    ActivityPlanInput,
    EntryChanges,
    GoalInput,
    ListEntriesInput,
    ListTrainingsInput,
    LogEntryInput,
    LogTrainingInput,
    NutritionValues,
    SummarizeInput,
    TrainingChanges,
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

SCHEMA_VERSION = 4
CREATE_RETRY_WINDOW = timedelta(minutes=10)
ENERGY_POLICY_ID = POLICY_ID
CONFIDENCE_MULTIPLIERS_PERMILLE = {"high": 1_000, "medium": 800, "low": 600}
RECOVERY_WEIGHTS_PERMILLE = (500, 300, 200)


class RepositoryError(RuntimeError):
    """Base class for expected persistence failures."""


class NotFoundError(RepositoryError):
    pass


class RevisionConflictError(RepositoryError):
    def __init__(
        self, entry_id: str, expected: int, current: int, *, record_type: str = "entry"
    ) -> None:
        super().__init__(
            f"revision conflict for {record_type} {entry_id}: "
            f"expected {expected}, current {current}"
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

MIGRATION_2 = """
ALTER TABLE daily_goals ADD COLUMN base_burn_mkcal INTEGER;
ALTER TABLE daily_goals ADD COLUMN deficit_mkcal INTEGER NOT NULL DEFAULT 0;
UPDATE daily_goals SET base_burn_mkcal = calories_mkcal WHERE base_burn_mkcal IS NULL;
UPDATE daily_goals SET calories_mkcal = NULL;

CREATE TABLE trainings (
    training_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    occurred_at TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    activity TEXT NOT NULL,
    duration_milliseconds INTEGER NOT NULL CHECK (duration_milliseconds > 0),
    calories_burned_mkcal INTEGER NOT NULL CHECK (calories_burned_mkcal > 0),
    source_type TEXT NOT NULL,
    source_detail TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE training_revisions (
    revision_id TEXT PRIMARY KEY,
    training_id TEXT NOT NULL REFERENCES trainings(training_id),
    resulting_revision INTEGER NOT NULL,
    operation TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE training_create_fingerprints (
    request_digest TEXT NOT NULL,
    training_id TEXT NOT NULL REFERENCES trainings(training_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE INDEX trainings_active_time_idx
    ON trainings(occurred_at_utc DESC, training_id DESC) WHERE deleted_at IS NULL;
CREATE INDEX training_fingerprints_digest_idx
    ON training_create_fingerprints(request_digest, created_at DESC);
"""

MIGRATION_3 = """
ALTER TABLE trainings ADD COLUMN confidence TEXT NOT NULL DEFAULT 'medium'
    CHECK (confidence IN ('high', 'medium', 'low'));
ALTER TABLE trainings ADD COLUMN measurement_method TEXT NOT NULL DEFAULT 'legacy_unspecified'
    CHECK (measurement_method IN (
        'indirect_calorimetry', 'power_meter', 'heart_rate_gps_model',
        'fitness_machine', 'device_estimate', 'manual_estimate',
        'legacy_unspecified', 'other'
    ));
ALTER TABLE trainings ADD COLUMN evidence_json TEXT;

UPDATE trainings SET
    confidence = CASE
        WHEN source_type = 'estimated' THEN 'low'
        ELSE 'medium'
    END,
    measurement_method = CASE source_type
        WHEN 'estimated' THEN 'manual_estimate'
        WHEN 'wearable' THEN 'device_estimate'
        WHEN 'fitness_machine' THEN 'fitness_machine'
        WHEN 'app' THEN 'device_estimate'
        ELSE 'legacy_unspecified'
    END;
"""


MIGRATION_4 = """
CREATE TABLE day_reviews (
    timezone TEXT NOT NULL,
    on_date TEXT NOT NULL,
    intake_complete INTEGER NOT NULL CHECK(intake_complete IN (0, 1)),
    exceptional_activity INTEGER NOT NULL CHECK(exceptional_activity IN (0, 1)),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(timezone, on_date)
);
CREATE TABLE day_review_revisions (
    revision_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL,
    record_key TEXT NOT NULL,
    resulting_revision INTEGER NOT NULL,
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
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
            if 2 not in versions:
                connection.executescript(MIGRATION_2)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _timestamp(self.clock())),
                )
            if 3 not in versions:
                connection.executescript(MIGRATION_3)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _timestamp(self.clock())),
                )

            if 4 not in versions:
                connection.executescript(MIGRATION_4)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, _timestamp(self.clock())),
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
    def _training_digest(request: LogTrainingInput) -> str:
        payload = request.model_dump(mode="json", exclude={"force_new"})
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode()).hexdigest()

    @staticmethod
    def _training_from_row(row: sqlite3.Row) -> dict[str, Any]:
        reported_burn_mkcal = int(row["calories_burned_mkcal"])
        multiplier = CONFIDENCE_MULTIPLIERS_PERMILLE[row["confidence"]]
        credited_burn_mkcal = (reported_burn_mkcal * multiplier + 500) // 1_000
        return {
            "policy_id": ENERGY_POLICY_ID,
            "training_id": row["training_id"],
            "revision": row["revision"],
            "occurred_at": row["occurred_at"],
            "timezone": row["timezone"],
            "activity": row["activity"],
            "duration_minutes": _unscale(row["duration_milliseconds"], 60_000),
            "reported_burn_kcal": _unscale(reported_burn_mkcal, 1_000),
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
        now = self.clock()
        now_text = _timestamp(now)
        digest = self._training_digest(request)
        cutoff = _timestamp(now - CREATE_RETRY_WINDOW)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
                    connection.commit()
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
            connection.commit()
            return {**training, "deduplicated": False}

    def get_training(self, training_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._get_training(connection, training_id)

    def update_training(
        self,
        training_id: str,
        expected_revision: int,
        reason: str,
        changes: TrainingChanges,
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
            return updated

    def delete_training(
        self, training_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        now_text = _timestamp(self.clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
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
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        has_more = len(rows) > request.limit
        page = rows[: request.limit]
        next_cursor = None
        if has_more and page:
            next_cursor = self._encode_cursor(page[-1]["occurred_at_utc"], page[-1]["training_id"])
        return {
            "trainings": [self._training_from_row(row) for row in page],
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
            training_rows = connection.execute(
                """
                SELECT * FROM trainings
                WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
                ORDER BY occurred_at_utc
                """,
                (_timestamp(resolved.start), _timestamp(resolved.end)),
            ).fetchall()
            trainings = [self._training_from_row(row) for row in training_rows]

        zone = ZoneInfo(resolved.timezone)
        groups: dict[str, list[dict[str, Any]]] = {}
        training_groups: dict[str, list[dict[str, Any]]] = {}
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
            balances = self._energy_balances(
                min(summary_dates), max(summary_dates), resolved.timezone
            )
            with self._connect() as connection:
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
        now_text = _timestamp(self.clock())
        nutrients = (
            {column: None for column, _factor in NUTRIENTS.values()}
            if request.targets is None
            else _nutrient_db_values(request.targets)
        )
        nutrients["calories_mkcal"] = None
        base_burn_mkcal = _scale(request.base_burn_kcal, 1_000)
        deficit_mkcal = _scale(request.deficit_kcal, 1_000)
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
            connection.commit()
            result["energy_budget"] = self.energy_balance(request.effective_from, request.timezone)
            return result

    def _day_reviews(self, timezone: str) -> dict[str, dict[str, Any]]:
        validate_timezone(timezone)
        with self._connect() as connection:
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
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

    @staticmethod
    def energy_policy() -> dict[str, Any]:
        return {
            "policy_id": ENERGY_POLICY_ID,
            "status": "active",
            "calculation_basis": "current_policy",
            "confidence_multipliers": {"high": 1.0, "medium": 0.8, "low": 0.6},
            "recovery_weights": [0.5, 0.3, 0.2],
            "recovery_pool_cap": "next_day_planned_deficit / first_recovery_weight",
            "recovery_pool_overflow": "repay_eligible_debit_then_expire",
            "daily_cap": "destination_planned_deficit",
            "collision_handling": "proportional",
            "overflow": "expire",
            "missed_allocation": "repay_opening_debit_on_settlement_then_expire",
            "ordinary_target_formula": "base_burn - deficit",
            "planned_baseline_formula": "ordinary_target + incoming_recovery - debit_adjustment",
            "available_ceiling_formula": ("planned_baseline + credited_training_burn"),
            "allowance_semantics": "optional_ceiling_not_intake_recommendation",
            "attribution_order": [
                "ordinary_target",
                "incoming_recovery",
                "same_day_exercise_allowance",
            ],
            "debit": {
                "effective_from": DEBIT_EFFECTIVE_FROM.isoformat(),
                "creation": "max(0, logged_intake - base_burn - credited_training_burn)",
                "missed_ordinary_deficit": "forgiven",
                "adjustment_start": "next_day_with_opening_debit_unless_activity_or_recovery_pause",
                "repeat_overshoots": "add_surplus_to_debit; never_stack_daily_adjustments",
                "daily_additional_deficit_cap_kcal": 200,
                "balance_cap": None,
                "expiry": None,
                "repayment": "unused_unadjusted_budget_after_source_recovery_reserve",
                "repayment_formula": (
                    "min(opening_debit, max(0, ordinary_target + incoming_recovery + "
                    "credited_exercise - intake - reserved_source_pool))"
                ),
                "repayment_daily_cap": None,
                "repayment_requires": "past_local_day; at_least_one_intake_entry; known_calories",
                "unsettled_surplus": "accrue_as_provisional_lower_bound",
                "projection": "ceil(remaining_debit / 200) eligible days, not a deadline",
                "review_after_projected_eligible_days": REVIEW_AFTER_ELIGIBLE_DAYS,
                "pauses": [
                    "exceptional_activity",
                    "day_after_exceptional_activity",
                    "protected_recovery",
                ],
                "pause_effect": "no_additional_restriction; actual_extra_deficit_can_still_repay",
            },
            "exceptional_activity": {
                "credited_burn_threshold_kcal": 1000,
                "total_duration_threshold_minutes": 180,
                "explicit_activity_plan_supported": True,
                "recovery_priority": "reserve_capped_pool_before_debit_repayment",
                "ordinary_carryover_with_debit": "suspended",
                "protected_carryover_with_debit": "honoured",
                "coefficients": "planning_conventions_not_physiological_requirements",
            },
            "day_settlement": {
                "timing": "past_local_calendar_days_on_query; no_timer_or_confirmation",
                "basis": "current_logged_intake; empty_or_unknown_calorie_days_cannot_repay",
                "corrections": (
                    "backdated_additions_edits_and_deletions_recalculate_subsequent_balances"
                ),
            },
            "document_ref": "docs/energy-credit-policy.md",
            "policy_text": Path(__file__).with_name("energy-credit-policy.md").read_text(),
        }

    @staticmethod
    def _proportional_cap(
        candidates: list[tuple[date, int, int]], cap_mkcal: int
    ) -> list[tuple[date, int, int, int]]:
        total = sum(candidate for _source, _offset, candidate in candidates)
        if total <= cap_mkcal:
            return [(*candidate, candidate[2]) for candidate in candidates]
        if total == 0 or cap_mkcal == 0:
            return [(*candidate, 0) for candidate in candidates]

        allocated: list[list[Any]] = []
        for source, offset, candidate in candidates:
            quotient, remainder = divmod(candidate * cap_mkcal, total)
            allocated.append([source, offset, candidate, quotient, remainder])
        remainder_units = cap_mkcal - sum(item[3] for item in allocated)
        allocation_order = sorted(
            range(len(allocated)),
            key=lambda index: (-allocated[index][4], allocated[index][0], allocated[index][1]),
        )
        for index in allocation_order[:remainder_units]:
            allocated[index][3] += 1
        return [(item[0], item[1], item[2], item[3]) for item in allocated]

    def _energy_balances(
        self, start_date: date, end_date: date, timezone: str
    ) -> dict[str, dict[str, Any]]:
        validate_timezone(timezone)
        zone = ZoneInfo(timezone)
        today = self.clock().astimezone(zone).date()
        with self._connect() as connection:
            goal_rows = connection.execute(
                """
                SELECT * FROM daily_goals
                WHERE timezone = ? AND effective_from <= ?
                ORDER BY effective_from
                """,
                (timezone, (end_date + timedelta(days=3)).isoformat()),
            ).fetchall()
            earliest_goal = (
                None if not goal_rows else date.fromisoformat(goal_rows[0]["effective_from"])
            )
            scan_start = start_date if earliest_goal is None else min(start_date, earliest_goal)
            scan_end = end_date + timedelta(days=3)
            start_utc = datetime.combine(scan_start, time.min, tzinfo=zone).astimezone(UTC)
            end_utc = datetime.combine(
                scan_end + timedelta(days=1), time.min, tzinfo=zone
            ).astimezone(UTC)
            entry_rows = connection.execute(
                """
                SELECT e.entry_id, e.occurred_at_utc,
                       SUM(c.calories_mkcal) AS calories_mkcal,
                       COUNT(*) AS component_count,
                       COUNT(c.calories_mkcal) AS known_component_count
                FROM entries AS e
                JOIN entry_components AS c ON c.entry_id = e.entry_id
                WHERE e.deleted_at IS NULL
                  AND e.occurred_at_utc >= ? AND e.occurred_at_utc < ?
                GROUP BY e.entry_id, e.occurred_at_utc
                """,
                (_timestamp(start_utc), _timestamp(end_utc)),
            ).fetchall()
            training_rows = connection.execute(
                """
                SELECT * FROM trainings
                WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
                """,
                (_timestamp(start_utc), _timestamp(end_utc)),
            ).fetchall()

        intake_by_day: dict[date, int] = {}
        intake_complete_by_day: dict[date, bool] = {}
        for row in entry_rows:
            local_date = _parse_timestamp(row["occurred_at_utc"]).astimezone(zone).date()
            intake_by_day[local_date] = intake_by_day.get(local_date, 0) + int(
                row["calories_mkcal"] or 0
            )
            complete = row["component_count"] == row["known_component_count"]
            intake_complete_by_day[local_date] = (
                intake_complete_by_day.get(local_date, True) and complete
            )

        trainings_by_day: dict[date, list[sqlite3.Row]] = {}
        for row in training_rows:
            local_date = _parse_timestamp(row["occurred_at_utc"]).astimezone(zone).date()
            trainings_by_day.setdefault(local_date, []).append(row)

        goals_by_date = {date.fromisoformat(row["effective_from"]): row for row in goal_rows}
        active_goals_by_date: dict[date, sqlite3.Row | None] = {}
        active_goal: sqlite3.Row | None = None
        goal_date = scan_start
        while goal_date <= scan_end:
            if goal_date in goals_by_date:
                active_goal = goals_by_date[goal_date]
            active_goals_by_date[goal_date] = active_goal
            goal_date += timedelta(days=1)

        reviews = self._day_reviews(timezone)
        outstanding_mkcal = 0
        unsettled_dates: list[str] = []
        pending: dict[date, list[tuple[date, int, int]]] = {}
        internal: dict[date, dict[str, Any]] = {}
        current_date = scan_start
        while current_date <= scan_end:
            active_goal = active_goals_by_date[current_date]
            goal = None if active_goal is None else self._goal_from_row(active_goal)
            base_mkcal = 0 if active_goal is None else int(active_goal["base_burn_mkcal"])
            deficit_mkcal = 0 if active_goal is None else int(active_goal["deficit_mkcal"])
            ordinary_mkcal = base_mkcal - deficit_mkcal

            debit_active = current_date >= DEBIT_EFFECTIVE_FROM
            opening_debit_mkcal = outstanding_mkcal
            candidates = pending.get(current_date, [])
            suppressed_recovery_mkcal = 0
            if debit_active and opening_debit_mkcal > 0:
                kept_candidates = []
                for candidate in candidates:
                    if internal[candidate[0]]["exceptional_activity"]:
                        kept_candidates.append(candidate)
                    else:
                        suppressed_recovery_mkcal += candidate[2]
                        internal[candidate[0]]["recovery_schedule"].append(
                            {
                                "date": current_date,
                                "day_offset": candidate[1],
                                "candidate_mkcal": candidate[2],
                                "scheduled_mkcal": 0,
                            }
                        )
                candidates = kept_candidates
            allocated = self._proportional_cap(candidates, deficit_mkcal)
            incoming_mkcal = sum(item[3] for item in allocated)
            for source_date, offset, candidate_mkcal, scheduled_mkcal in allocated:
                source_schedule = internal[source_date]["recovery_schedule"]
                source_schedule.append(
                    {
                        "date": current_date,
                        "day_offset": offset,
                        "candidate_mkcal": candidate_mkcal,
                        "scheduled_mkcal": scheduled_mkcal,
                    }
                )

            day_trainings = trainings_by_day.get(current_date, [])
            reported_mkcal = sum(int(row["calories_burned_mkcal"]) for row in day_trainings)
            credited_mkcal = sum(
                (
                    int(row["calories_burned_mkcal"])
                    * CONFIDENCE_MULTIPLIERS_PERMILLE[row["confidence"]]
                    + 500
                )
                // 1_000
                for row in day_trainings
            )
            intake_mkcal = intake_by_day.get(current_date, 0)
            intake_complete = intake_complete_by_day.get(current_date, True)
            provisional = current_date >= today
            review = reviews.get(current_date.isoformat(), {})
            intake_logged = current_date in intake_by_day
            exceptional = (
                credited_mkcal >= EXCEPTIONAL_BURN_MKCAL
                or sum(int(row["duration_milliseconds"]) for row in day_trainings)
                >= EXCEPTIONAL_DURATION_MS
                or bool(review.get("exceptional_activity", False))
            )
            previous_exceptional = internal.get(current_date - timedelta(days=1), {}).get(
                "exceptional_activity", False
            )
            protected_incoming = sum(
                item[3] for item in allocated if internal[item[0]]["exceptional_activity"]
            )
            pause_reasons = []
            if exceptional:
                pause_reasons.append("exceptional_activity")
            if previous_exceptional:
                pause_reasons.append("day_after_exceptional_activity")
            if protected_incoming:
                pause_reasons.append("protected_recovery")
            if deficit_mkcal == 0:
                pause_reasons.append("no_planned_deficit")
            adjustment_mkcal = (
                min(DAILY_ADJUSTMENT_MKCAL, opening_debit_mkcal, max(0, ordinary_mkcal))
                if debit_active and not pause_reasons and goal is not None
                else 0
            )
            status = "ok"
            exercise_used_mkcal: int | None = None
            unused_exercise_mkcal: int | None = None
            incoming_used_mkcal: int | None = None
            recovery_pool_cap_mkcal: int | None = None
            recovery_pool_mkcal: int | None = None
            if goal is None:
                status = "no_goal"
            elif not intake_complete:
                status = "incomplete_intake"
            else:
                incoming_used_mkcal = min(max(intake_mkcal - ordinary_mkcal, 0), incoming_mkcal)
                exercise_used_mkcal = min(
                    max(intake_mkcal - ordinary_mkcal - incoming_mkcal, 0),
                    credited_mkcal,
                )
                unused_exercise_mkcal = credited_mkcal - exercise_used_mkcal
                next_goal = active_goals_by_date.get(current_date + timedelta(days=1))
                next_deficit_mkcal = 0 if next_goal is None else int(next_goal["deficit_mkcal"])
                recovery_pool_cap_mkcal = next_deficit_mkcal * 1_000 // RECOVERY_WEIGHTS_PERMILLE[0]
                recovery_pool_mkcal = min(unused_exercise_mkcal, recovery_pool_cap_mkcal)
                # Ordinary carryover yields to outstanding debit. Exceptional recovery is protected.
                if debit_active and opening_debit_mkcal > 0 and not exceptional:
                    recovery_pool_mkcal = 0

            debit_added_mkcal = 0
            debit_repaid_mkcal = 0
            repayment_from_exercise_mkcal = 0
            repayment_from_incoming_mkcal = 0
            repayment_eligible = (
                debit_active
                and current_date < today
                and intake_logged
                and intake_complete
                and goal is not None
            )
            if debit_active and current_date <= today and goal is not None:
                debit_added_mkcal = max(0, intake_mkcal - base_mkcal - credited_mkcal)
                if not intake_logged or not intake_complete or provisional:
                    unsettled_dates.append(current_date.isoformat())
                if repayment_eligible:
                    # Repay unused unadjusted budget, including incoming recovery.
                    extra_deficit = max(
                        0,
                        ordinary_mkcal
                        + credited_mkcal
                        + incoming_mkcal
                        - intake_mkcal
                        - (recovery_pool_mkcal or 0),
                    )
                    debit_repaid_mkcal = min(opening_debit_mkcal, extra_deficit)
                    repayment_from_exercise_mkcal = min(
                        debit_repaid_mkcal,
                        max(0, (unused_exercise_mkcal or 0) - (recovery_pool_mkcal or 0)),
                    )
                    repayment_from_incoming_mkcal = min(
                        debit_repaid_mkcal - repayment_from_exercise_mkcal,
                        incoming_mkcal - (incoming_used_mkcal or 0),
                    )
                    # If this day clears debit, remaining ordinary exercise credit can recover.
                    if not exceptional and debit_repaid_mkcal == opening_debit_mkcal:
                        recovery_pool_mkcal = min(
                            max(0, (unused_exercise_mkcal or 0) - repayment_from_exercise_mkcal),
                            recovery_pool_cap_mkcal or 0,
                        )
                outstanding_mkcal = opening_debit_mkcal + debit_added_mkcal - debit_repaid_mkcal

            internal[current_date] = {
                "date": current_date,
                "status": status,
                "provisional": provisional,
                "goal": goal,
                "base_mkcal": base_mkcal,
                "deficit_mkcal": deficit_mkcal,
                "ordinary_mkcal": ordinary_mkcal,
                "intake_mkcal": intake_mkcal,
                "intake_complete": intake_complete,
                "reported_mkcal": reported_mkcal,
                "credited_mkcal": credited_mkcal,
                "incoming_mkcal": incoming_mkcal,
                "incoming_used_mkcal": incoming_used_mkcal,
                "incoming_repaid_mkcal": repayment_from_incoming_mkcal,
                "exercise_used_mkcal": exercise_used_mkcal,
                "unused_exercise_mkcal": unused_exercise_mkcal,
                "recovery_pool_cap_mkcal": recovery_pool_cap_mkcal,
                "recovery_pool_mkcal": recovery_pool_mkcal,
                "recovery_schedule": [],
                "exceptional_activity": exceptional,
                "intake_logged": intake_logged,
                "protected_incoming_mkcal": protected_incoming,
                "recovery_source_provisional": provisional
                or not intake_complete
                or not intake_logged,
                "incoming_recovery_sources": [
                    {
                        "source_date": source.isoformat(),
                        "scheduled_kcal": _unscale(amount, 1_000),
                        "protected": internal[source]["exceptional_activity"],
                        "provisional": internal[source]["recovery_source_provisional"],
                    }
                    for source, _offset, _candidate, amount in allocated
                ],
                "suppressed_recovery_mkcal": suppressed_recovery_mkcal,
                "opening_debit_mkcal": opening_debit_mkcal,
                "debit_added_mkcal": debit_added_mkcal,
                "debit_repaid_mkcal": debit_repaid_mkcal,
                "closing_debit_mkcal": outstanding_mkcal,
                "repayment_from_exercise_mkcal": repayment_from_exercise_mkcal,
                "adjustment_mkcal": adjustment_mkcal,
                "pause_reasons": pause_reasons,
                "repayment_eligible": repayment_eligible,
                "unsettled_dates": list(unsettled_dates),
            }
            if recovery_pool_mkcal is not None and recovery_pool_mkcal > 0:
                first = (recovery_pool_mkcal * RECOVERY_WEIGHTS_PERMILLE[0] + 500) // 1_000
                second = (recovery_pool_mkcal * RECOVERY_WEIGHTS_PERMILLE[1] + 500) // 1_000
                amounts = (first, second, recovery_pool_mkcal - first - second)
                for offset, amount in enumerate(amounts, start=1):
                    pending.setdefault(current_date + timedelta(days=offset), []).append(
                        (current_date, offset, amount)
                    )
            current_date += timedelta(days=1)

        results: dict[str, dict[str, Any]] = {}
        current_date = start_date
        while current_date <= end_date:
            item = internal[current_date]
            schedule = sorted(item["recovery_schedule"], key=lambda value: value["day_offset"])
            scheduled_total = sum(value["scheduled_mkcal"] for value in schedule)
            unused_exercise = item["unused_exercise_mkcal"]
            recovery_pool = item["recovery_pool_mkcal"]
            incoming_remaining = (
                None
                if item["incoming_used_mkcal"] is None
                else item["incoming_mkcal"] - item["incoming_used_mkcal"]
            )
            results[current_date.isoformat()] = {
                "policy_id": ENERGY_POLICY_ID,
                "calculation_basis": "current_policy",
                "date": current_date.isoformat(),
                "timezone": timezone,
                "status": item["status"],
                "provisional": item["provisional"],
                "base_burn_kcal": _unscale(item["base_mkcal"], 1_000),
                "deficit_kcal": _unscale(item["deficit_mkcal"], 1_000),
                "ordinary_target_kcal": _unscale(item["ordinary_mkcal"], 1_000),
                "intake_kcal": _unscale(item["intake_mkcal"], 1_000),
                "intake_complete": item["intake_complete"],
                "day_closed": not item["provisional"],
                "intake_logged": item["intake_logged"],
                "intake_settled": not item["recovery_source_provisional"],
                "exceptional_activity": item["exceptional_activity"],
                "protected_incoming_recovery_kcal": _unscale(
                    item["protected_incoming_mkcal"], 1_000
                ),
                "suppressed_recovery_kcal": _unscale(item["suppressed_recovery_mkcal"], 1_000),
                "debit": {
                    "active": current_date >= DEBIT_EFFECTIVE_FROM,
                    "opening_kcal": _unscale(item["opening_debit_mkcal"], 1_000),
                    "added_kcal": _unscale(item["debit_added_mkcal"], 1_000),
                    "repaid_kcal": _unscale(item["debit_repaid_mkcal"], 1_000),
                    "closing_kcal": _unscale(item["closing_debit_mkcal"], 1_000),
                    "additional_deficit_kcal": _unscale(item["adjustment_mkcal"], 1_000),
                    "pause_reasons": item["pause_reasons"],
                    "repayment_eligible": item["repayment_eligible"],
                    "balance_provisional": bool(item["unsettled_dates"]),
                    "unsettled_dates": item["unsettled_dates"],
                    "projected_eligible_days": (
                        item["closing_debit_mkcal"] + DAILY_ADJUSTMENT_MKCAL - 1
                    )
                    // DAILY_ADJUSTMENT_MKCAL,
                    "review_recommended": item["closing_debit_mkcal"]
                    > (REVIEW_AFTER_ELIGIBLE_DAYS * DAILY_ADJUSTMENT_MKCAL),
                },
                "reported_training_burn_kcal": _unscale(item["reported_mkcal"], 1_000),
                "credited_training_burn_kcal": _unscale(item["credited_mkcal"], 1_000),
                "incoming_recovery_kcal": _unscale(item["incoming_mkcal"], 1_000),
                "incoming_recovery_sources": item["incoming_recovery_sources"],
                "incoming_recovery_used_kcal": _unscale(item["incoming_used_mkcal"], 1_000),
                "incoming_recovery_remaining_kcal": _unscale(incoming_remaining, 1_000),
                "incoming_recovery_repaid_kcal": _unscale(item["incoming_repaid_mkcal"], 1_000),
                "incoming_recovery_expired_kcal": (
                    _unscale(
                        None
                        if incoming_remaining is None
                        else incoming_remaining - item["incoming_repaid_mkcal"],
                        1_000,
                    )
                    if current_date < today
                    else None
                ),
                "planned_baseline_kcal": (
                    None
                    if item["goal"] is None
                    else _unscale(
                        item["ordinary_mkcal"] + item["incoming_mkcal"] - item["adjustment_mkcal"],
                        1_000,
                    )
                ),
                "available_ceiling_kcal": (
                    None
                    if item["goal"] is None
                    else _unscale(
                        item["ordinary_mkcal"]
                        + item["incoming_mkcal"]
                        + item["credited_mkcal"]
                        - item["adjustment_mkcal"],
                        1_000,
                    )
                ),
                "exercise_credit_used_kcal": _unscale(item["exercise_used_mkcal"], 1_000),
                "unused_exercise_credit_kcal": _unscale(unused_exercise, 1_000),
                "recovery_pool_cap_kcal": _unscale(item["recovery_pool_cap_mkcal"], 1_000),
                "recovery_pool_kcal": _unscale(recovery_pool, 1_000),
                "exercise_credit_excluded_from_recovery_kcal": (
                    None
                    if unused_exercise is None or recovery_pool is None
                    else _unscale(unused_exercise - recovery_pool, 1_000)
                ),
                "recovery_schedule": [
                    {
                        "date": value["date"].isoformat(),
                        "day_offset": value["day_offset"],
                        "candidate_kcal": _unscale(value["candidate_mkcal"], 1_000),
                        "provisional": item["recovery_source_provisional"],
                        "protected": item["exceptional_activity"],
                        "scheduled_kcal": _unscale(value["scheduled_mkcal"], 1_000),
                        "expired_kcal": _unscale(
                            value["candidate_mkcal"] - value["scheduled_mkcal"], 1_000
                        ),
                    }
                    for value in schedule
                ],
                "recovery_scheduled_kcal": _unscale(scheduled_total, 1_000),
                "recovery_pool_expired_at_creation_kcal": (
                    None
                    if recovery_pool is None
                    else _unscale(recovery_pool - scheduled_total, 1_000)
                ),
                "exercise_credit_used_for_debit_kcal": _unscale(
                    item["repayment_from_exercise_mkcal"], 1_000
                ),
                "exercise_credit_expired_at_creation_kcal": (
                    None
                    if unused_exercise is None
                    else _unscale(
                        unused_exercise - scheduled_total - item["repayment_from_exercise_mkcal"],
                        1_000,
                    )
                ),
            }
            current_date += timedelta(days=1)
        return results

    def energy_balance(self, on_date: date, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
        return self._energy_balances(on_date, on_date, timezone)[on_date.isoformat()]

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
        current_goal = None if current is None else self._goal_from_row(current)
        if current_goal is not None:
            current_goal["energy_budget"] = self.energy_balance(effective_date, timezone)
        return {
            "policy_id": ENERGY_POLICY_ID,
            "on_date": effective_date.isoformat(),
            "timezone": timezone,
            "current": current_goal,
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

    def training_revision_history(self, training_id: str) -> list[dict[str, Any]]:
        """Internal helper used by tests and future operator tooling."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resulting_revision, operation, reason, snapshot_json, created_at
                FROM training_revisions WHERE training_id = ? ORDER BY resulting_revision
                """,
                (training_id,),
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
