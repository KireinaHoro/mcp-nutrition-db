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
