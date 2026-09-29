from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_nutrition_db import cli


class InterruptingServer:
    def run(self, *, transport: str) -> None:
        assert transport == "stdio"
        raise KeyboardInterrupt


def test_completed_ctrl_c_shutdown_exits_cleanly(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "create_server", lambda *args, **kwargs: InterruptingServer())

    result = cli.main(
        ["serve", "--database", str(tmp_path / "nutrition.sqlite3"), "--transport", "stdio"]
    )

    assert result == 0
    assert not (tmp_path / "nutrition.sqlite3-wal").exists()


def test_serve_owns_wal_sidecars_until_shutdown(monkeypatch, tmp_path):
    database = tmp_path / "nutrition.sqlite3"
    wal = Path(str(database) + "-wal")

    class Server:
        def run(self, *, transport):
            assert transport == "stdio"
            assert wal.exists()

    monkeypatch.setattr("mcp_nutrition_db.cli.create_server", lambda *args, **kwargs: Server())
    assert cli.main(["serve", "--transport", "stdio", "--database", str(database)]) == 0
    assert not wal.exists()
