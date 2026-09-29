"""Numbered schema changes. Published migration SQL is immutable."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .serialization import timestamp

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


MIGRATION_5 = """
CREATE TABLE foods (
    food_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    status TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE food_revisions (
    food_id TEXT NOT NULL REFERENCES foods(food_id),
    revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(food_id, revision)
);
CREATE TABLE food_identity_keys (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    food_id TEXT NOT NULL REFERENCES foods(food_id),
    PRIMARY KEY(kind, value)
);
CREATE INDEX food_identity_owner_idx ON food_identity_keys(food_id);
CREATE TABLE food_search_terms (
    term TEXT NOT NULL,
    food_id TEXT NOT NULL REFERENCES foods(food_id),
    PRIMARY KEY(term, food_id)
);
ALTER TABLE entry_components ADD COLUMN food_id TEXT;
ALTER TABLE entry_components ADD COLUMN food_revision INTEGER;
ALTER TABLE entry_components ADD COLUMN inventory_json TEXT;
ALTER TABLE entry_components ADD COLUMN source_evidence_json TEXT;
CREATE INDEX component_food_idx ON entry_components(food_id, food_revision);
CREATE TRIGGER component_food_insert BEFORE INSERT ON entry_components
WHEN (NEW.food_id IS NULL) != (NEW.food_revision IS NULL)
 OR (NEW.food_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM food_revisions WHERE food_id=NEW.food_id AND revision=NEW.food_revision
 )) BEGIN SELECT RAISE(ABORT, 'invalid food revision reference'); END;
CREATE TRIGGER component_food_update BEFORE UPDATE OF food_id, food_revision ON entry_components
WHEN (NEW.food_id IS NULL) != (NEW.food_revision IS NULL)
 OR (NEW.food_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM food_revisions WHERE food_id=NEW.food_id AND revision=NEW.food_revision
 )) BEGIN SELECT RAISE(ABORT, 'invalid food revision reference'); END;
CREATE TABLE food_link_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    result_json TEXT
);
CREATE TABLE usda_lookups (
    lookup_id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE usda_snapshots (
    source_snapshot_id TEXT PRIMARY KEY,
    fdc_id INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX usda_snapshot_id_time_idx ON usda_snapshots(fdc_id, created_at DESC);
"""

MIGRATIONS = (MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(connection: sqlite3.Connection, now: datetime) -> None:
    """Serialize startup and commit each schema change with its version marker."""
    for version, script in enumerate(MIGRATIONS, start=1):
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if applied - set(range(1, SCHEMA_VERSION + 1)):
                raise ValueError("database schema is newer than this application")
            if version in applied:
                continue
            statement = ""
            for line in script.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError("incomplete migration statement")
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)", (version, timestamp(now))
            )
