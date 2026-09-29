from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from mcp_nutrition_db import migrations
from mcp_nutrition_db.database import Database


@pytest.mark.parametrize("version", range(1, migrations.SCHEMA_VERSION + 1))
def test_failed_migration_rolls_back_schema_and_marker_and_can_retry(
    tmp_path, monkeypatch, version
):
    database = Database(tmp_path / "migration.sqlite3")
    registry = migrations.MIGRATIONS
    with database.connection() as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
    monkeypatch.setattr(migrations, "MIGRATIONS", registry[: version - 1])
    database.migrate()
    with database.connection() as connection:
        before = list(connection.iterdump())
    broken = (*registry[: version - 1], registry[version - 1] + "\nINVALID SQL;\n")
    monkeypatch.setattr(migrations, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        database.migrate()
    with database.connection() as connection:
        assert list(connection.iterdump()) == before
    monkeypatch.setattr(migrations, "MIGRATIONS", registry)
    database.migrate()
    database.migrate()
    assert database.schema_version() == migrations.SCHEMA_VERSION
    with database.connection() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_connection_closes_on_success_and_failure_and_rolls_back(tmp_path):
    database = Database(tmp_path / "connection.sqlite3")
    database.migrate()
    with database.connection(write=True) as connection:
        connection.execute("CREATE TABLE fixture (value INTEGER)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="rollback"), database.connection(write=True) as failed:
        failed.execute("INSERT INTO fixture VALUES (1)")
        raise RuntimeError("rollback")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed.execute("SELECT 1")
    with database.connection() as check:
        assert check.execute("SELECT count(*) FROM fixture").fetchone()[0] == 0


def test_concurrent_migrations_are_serialized(tmp_path):
    database = Database(
        tmp_path / "concurrent.sqlite3", clock=lambda: datetime(2026, 9, 29, tzinfo=UTC)
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: database.migrate(), range(4)))
    assert database.schema_version() == migrations.SCHEMA_VERSION


def test_newer_schema_is_rejected_without_changes(tmp_path):
    database = Database(tmp_path / "future.sqlite3")
    database.migrate()
    with database.connection(write=True) as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (99, 'future')")
    with pytest.raises(ValueError, match="newer"):
        database.migrate()
    assert database.schema_version() == 99
