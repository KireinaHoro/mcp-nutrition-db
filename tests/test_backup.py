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
