"""Command-line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import os
from collections.abc import Sequence
from pathlib import Path

from .models import DEFAULT_TIMEZONE
from .observability import configure_logging
from .repository import NutritionRepository
from .server import create_server


def _loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return address == "localhost"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-nutrition-db",
        description="Private MCP server for a conversational nutrition log",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="migrate the database and run the MCP server")
    serve.add_argument(
        "--database",
        default=os.environ.get("MCP_NUTRITION_DB_PATH", "./nutrition.sqlite3"),
        help="SQLite database path (default: MCP_NUTRITION_DB_PATH or ./nutrition.sqlite3)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    serve.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default=os.environ.get("MCP_NUTRITION_LOG_LEVEL", "info"),
    )
    serve.add_argument(
        "--transport", choices=("streamable-http", "stdio"), default="streamable-http"
    )
    serve.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="allow an HTTP listener outside loopback (unsafe; not needed for the tunnel)",
    )

    migrate = subparsers.add_parser("migrate", help="apply database migrations and exit")
    migrate.add_argument(
        "--database",
        default=os.environ.get("MCP_NUTRITION_DB_PATH", "./nutrition.sqlite3"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = Path(args.database).expanduser()
    repository = NutritionRepository(database)

    if args.command == "migrate":
        print(f"schema version {repository.schema_version()}")
        return 0

    configure_logging(args.log_level)

    if (
        args.transport == "streamable-http"
        and not args.allow_non_loopback
        and not _loopback(args.host)
    ):
        raise SystemExit(
            "refusing non-loopback listener; use --allow-non-loopback only with an explicit "
            "external authentication and threat model"
        )
    server = create_server(
        repository,
        host=args.host,
        port=args.port,
        default_timezone=args.timezone,
    )
    try:
        server.run(transport=args.transport)
    except KeyboardInterrupt:
        # FastMCP completes its application shutdown before propagating Ctrl-C.
        # Treat that completed operator shutdown as a clean exit.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
