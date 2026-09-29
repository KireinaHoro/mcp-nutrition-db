"""Operator CLI: private login, validation, sync, reconciliation and activation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from .backup import backup_database
from .body import BodyRepository
from .garmin import GarminAdapter
from .garmin_auth import login, private_write, resume, seed_session, session_lock
from .garmin_import import GarminImporter
from .garmin_reconcile import apply, prepare
from .repository import NutritionRepository
from .serialization import canonical_json, new_id


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("garmin", help="Garmin Connect import and private onboarding")
    parser.add_argument(
        "--database", default=os.environ.get("MCP_NUTRITION_DB_PATH", "./nutrition.sqlite3")
    )
    parser.add_argument("--state-directory", type=Path, default=Path("./.garmin-state"))
    parser.add_argument("--credentials-file", type=Path)
    commands = parser.add_subparsers(dest="garmin_command", required=True)
    command = commands.add_parser("login")
    command.add_argument("--output", required=True, type=Path)
    commands.add_parser("status")
    commands.add_parser(
        "validate", help="interactively attest account fields after inspecting a dry run"
    )
    command = commands.add_parser("sync")
    command.add_argument("--start", type=date.fromisoformat)
    command.add_argument("--end", type=date.fromisoformat)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--weekly", action="store_true")
    command.add_argument("--output", type=Path)
    command = commands.add_parser("reconcile")
    command.add_argument("--output", type=Path)
    command.add_argument(
        "--decisions", type=Path, help="prepare concrete preview from an edited report"
    )
    command.add_argument("--apply-plan", help="apply the stored, user-approved concrete preview")
    command.add_argument("--approved", action="store_true")
    command.add_argument("--backup", type=Path)
    commands.add_parser("activate")


def run(args: argparse.Namespace) -> int:
    if args.garmin_command == "login":
        login(args.output)
        print("Private session exported. Encrypt it with sops before tracking it in Git.")
        return 0
    state = args.state_directory.expanduser()
    with session_lock(state):
        # Back up before the constructor can migrate an existing database.
        database = Path(args.database)
        if database.exists():
            import sqlite3
            from contextlib import closing

            from .migrations import SCHEMA_VERSION

            with closing(
                sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
            ) as connection:
                version = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
            if version < SCHEMA_VERSION:
                backup_database(database, state / f"before-migration-{new_id()}.sqlite3")
        repository = NutritionRepository(database)
        if args.garmin_command == "status":
            print(canonical_json(BodyRepository(repository.database).sync_status()))
            return 0
        metadata = seed_session(state, args.credentials_file)
        # Reports and previews are offline; applying an approval rechecks remote facts.
        importer = GarminImporter(repository, GarminAdapter(None, metadata["account_id"]))
        if not getattr(args, "dry_run", False):
            importer.register()
        try:
            if args.garmin_command in ("sync", "validate") or (
                args.garmin_command == "reconcile" and args.apply_plan
            ):
                api = resume(state, metadata)
                importer = GarminImporter(repository, GarminAdapter(api, metadata["account_id"]))
                if not getattr(args, "dry_run", False):
                    with repository.database.connection(write=True) as connection:
                        connection.execute(
                            "UPDATE garmin_accounts SET auth_state='ready' WHERE account_id=?",
                            (metadata["account_id"],),
                        )
            if args.garmin_command == "sync":
                result = importer.sync(
                    start=args.start, end=args.end, dry_run=args.dry_run, weekly=args.weekly
                )
                if args.dry_run:
                    # Include only whitelisted relevant facts in the private verification report.
                    from datetime import timedelta

                    from .garmin import normalize
                    from .garmin_import import ZONE

                    end = args.end or repository.clock().astimezone(ZONE).date()
                    start = args.start or end - timedelta(days=7)
                    result["verification_facts"] = {
                        "weights": [
                            normalize("weight", r, importer.validation())
                            for r in importer.provider.weights(start, end)
                        ],
                        "activities": [
                            normalize("activity", r, importer.validation())
                            for r in importer.provider.activities(start, end)
                        ],
                    }
                if not args.dry_run:
                    with repository.database.connection(write=True) as connection:
                        connection.execute(
                            "UPDATE garmin_accounts SET last_error=NULL WHERE account_id=?",
                            (metadata["account_id"],),
                        )
                if args.output:
                    private_write(args.output, canonical_json(result), replace=False)
                    print(f"Private report written to {args.output}")
                else:
                    print(
                        canonical_json(
                            {k: v for k, v in result.items() if k != "verification_facts"}
                        )
                    )
            elif args.garmin_command == "validate":
                if not os.isatty(0):
                    raise ValueError("validation requires interactive account comparison")
                print(
                    "Compare the private dry-run facts with Garmin Connect. Answer yes only for "
                    "verified fields."
                )
                validation: dict[str, Any] = {
                    k: bool(metadata.get(k))
                    for k in ("login_validated", "session_restart_validated")
                }
                questions = {
                    "weight_grams_epoch_ms": (
                        "Are samplePk stable IDs, date UTC epoch milliseconds and weight "
                        "integer grams, matching your scale readings?"
                    ),
                    "activity_ids_utc_seconds": (
                        "Are activityId permanent IDs, startTimeGMT UTC, and "
                        "movingDuration/duration seconds, matching recorded workouts?"
                    ),
                    "explicit_active_calories": (
                        "Does activity activeCalories match the per-workout ACTIVE "
                        "calories for representative cycling and walking/running records?"
                    ),
                    "total_minus_resting": (
                        "Does activity calories minus bmrCalories match per-workout ACTIVE "
                        "calories for representative cycling and walking/running records?"
                    ),
                }
                for key, question in questions.items():
                    validation[key] = input(question + " [yes/no] ").strip().lower() == "yes"
                # Physical power-meter provenance is deliberately never inferred from power numbers.
                devices = input(
                    "Verified physical power-meter cycling device IDs, comma-separated; blank "
                    "for none: "
                )
                validation["power_meter_device_ids"] = [
                    d.strip() for d in devices.split(",") if d.strip()
                ]
                types = input(
                    "Verified HR/GPS model activity types (comma-separated; blank for none): "
                )
                validation["hr_gps_activity_types"] = [
                    v.strip() for v in types.split(",") if v.strip()
                ]
                importer.validate(validation)
                print(
                    "Validation recorded. Sync again to normalize staged facts; training "
                    "remains gated."
                )
            elif args.garmin_command == "reconcile":
                if args.apply_plan:
                    if args.backup is None:
                        raise ValueError("--backup is required before applying reconciliation")
                    result = apply(
                        importer, args.apply_plan, approved=args.approved, backup=args.backup
                    )
                    print(
                        canonical_json(
                            {"plan_id": result["plan_id"], "applied_at": result["applied_at"]}
                        )
                    )
                else:
                    if not args.output:
                        raise ValueError("--output private report path is required")
                    result = (
                        prepare(importer, json.loads(args.decisions.read_text()))
                        if args.decisions
                        else importer.report()
                    )
                    private_write(args.output, canonical_json(result), replace=False)
                    print(f"Private reconciliation report written to {args.output}")
            elif args.garmin_command == "activate":
                print(canonical_json(importer.activate()))
        except Exception as error:
            # Do not persist provider messages, URLs, account data, or response bodies.
            if args.garmin_command in ("sync", "validate") and not getattr(args, "dry_run", False):
                current = json.loads((state / "account.json").read_text())
                if "Authentication" in type(error).__name__:
                    current["auth_state"] = "reauth_required"
                    private_write(state / "account.json", canonical_json(current))
                if "TooManyRequests" in type(error).__name__:
                    from datetime import timedelta

                    from .serialization import timestamp

                    current["next_attempt_at"] = timestamp(repository.clock() + timedelta(hours=1))
                    private_write(state / "account.json", canonical_json(current))
                failure = (
                    "rate_limited" if "TooManyRequests" in type(error).__name__ else "sync_failed"
                )
                with repository.database.connection(write=True) as connection:
                    connection.execute(
                        "UPDATE garmin_accounts SET auth_state=?,last_error=? WHERE account_id=?",
                        (current.get("auth_state", "ready"), failure, metadata["account_id"]),
                    )
            raise
    return 0
