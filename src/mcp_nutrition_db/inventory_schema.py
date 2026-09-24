"""Additive catalog migration; never rewrites existing food history."""

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
