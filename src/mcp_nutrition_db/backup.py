"""SQLite online backup support."""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Atomically create and validate a consistent SQLite backup."""

    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("backup destination must differ from source database")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = destination_path.with_name(f".{destination_path.name}.{uuid.uuid4().hex}.tmp")
    source_uri = f"{source_path.as_uri()}?mode=ro"

    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True, timeout=30.0)) as source_db,
            closing(sqlite3.connect(temporary_path, timeout=30.0)) as backup_db,
        ):
            source_db.backup(backup_db)
            check = backup_db.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError("SQLite backup integrity check failed")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return destination_path
