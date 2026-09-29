from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_nutrition_db.backup import backup_database
from mcp_nutrition_db.repository import NutritionRepository


def test_online_backup_is_complete_atomic_and_private(
    repository: NutritionRepository, meal: object, tmp_path: Path
) -> None:
    created = repository.create_entry(meal)  # type: ignore[arg-type]
    destination = tmp_path / "backup" / "nutrition.sqlite3"
    destination.parent.mkdir()
    destination.write_text("old incomplete backup")

    result = backup_database(repository.database_path, destination)

    assert result == destination
    assert destination.stat().st_mode & 0o777 == 0o600
    restored = NutritionRepository(destination)
    assert restored.get_entry(created["entry_id"])["title"] == "Rice bowl with salmon"
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_failed_backup_preserves_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / "nutrition.sqlite3"
    destination.write_bytes(b"previous backup")

    with pytest.raises(sqlite3.OperationalError):
        backup_database(tmp_path / "missing.sqlite3", destination)

    assert destination.read_bytes() == b"previous backup"


def test_server_lifetime_keeps_wal_readable_in_read_only_backup_directory(tmp_path):
    from contextlib import closing

    from mcp_nutrition_db.database import Database

    state = tmp_path / "state"
    state.mkdir()
    database = Database(state / "nutrition.sqlite3")
    database.migrate()
    assert not Path(database.path + "-wal").exists()
    destination = tmp_path / "snapshot.sqlite3"
    with database.keep_wal_available():
        # A committed write remains in the WAL while the server is alive.
        with database.connection(write=True) as writer:
            writer.execute("CREATE TABLE backup_fixture (value INTEGER)")
            writer.execute("INSERT INTO backup_fixture VALUES (17)")
        assert Path(database.path + "-wal").stat().st_size > 0
        state.chmod(0o500)
        try:
            backup_database(database.path, destination)
        finally:
            state.chmod(0o700)
        with closing(sqlite3.connect(destination)) as snapshot:
            assert snapshot.execute("SELECT value FROM backup_fixture").fetchone()[0] == 17
    assert not Path(database.path + "-wal").exists()
    assert not Path(database.path + "-shm").exists()
