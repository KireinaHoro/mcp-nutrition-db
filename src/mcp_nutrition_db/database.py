"""SQLite connection ownership and explicit transaction boundaries."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from .migrations import migrate
from .serialization import utc_now


class Database:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        if str(path) == ":memory:":
            raise ValueError("a file-backed database is required")
        self.path = str(path)
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection

    def migrate(self) -> None:
        with self.connection() as connection:
            migrate(connection, self.clock())
            # WAL is persistent. Configure it at startup, not on every read.
            # SQLite can return BUSY immediately when another startup holds a
            # schema lock; retry without holding a transaction of our own.
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as error:
                    if (
                        error.sqlite_errorcode != sqlite3.SQLITE_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.02)

    def schema_version(self) -> int:
        with self.connection() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)
