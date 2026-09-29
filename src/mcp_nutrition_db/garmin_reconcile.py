"""Private, reproducible reconciliation previews and revision-checked application."""

from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from .backup import backup_database
from .energy_repository import load_energy_balances
from .garmin import normalize
from .garmin_import import ZONE, GarminImporter
from .repository import NutritionRepository
from .serialization import canonical_json, new_id, parse_timestamp, timestamp


def prepare(importer: GarminImporter, decisions: dict[str, Any]) -> dict[str, Any]:
    with importer.database.connection() as connection:
        snapshot = importer.snapshot(connection)
    if snapshot != decisions["snapshot"] or decisions["account_id"] != importer.account_id:
        raise ValueError("report is stale; regenerate it")
    if set(decisions["local_decisions"]) != {r["training_id"] for r in snapshot["trainings"]}:
        raise ValueError("report must cover every local training, including deleted records")
    if set(decisions.get("weight_decisions", {})) != {
        w["external_id"] for w in snapshot["weights"]
    }:
        raise ValueError("report must cover every staged weight")
    if sorted(i["external_id"] for i in decisions["activities"]) != sorted(
        r["external_id"] for r in snapshot["sources"]
    ):
        raise ValueError("report must cover every staged activity exactly once")
    now = importer.database.clock()
    dates = [
        parse_timestamp(r["occurred_at_utc"]).astimezone(ZONE).date() for r in snapshot["trainings"]
    ]
    dates += [
        parse_timestamp(i["facts"]["occurred_at"]).astimezone(ZONE).date()
        for i in decisions["activities"]
        if i["facts"].get("occurred_at")
    ]
    lower = min(dates, default=now.astimezone(ZONE).date())
    upper = max([*dates, now.astimezone(ZONE).date()]) + timedelta(days=3)
    # Simulation operates on an integrity-checked private copy, using the same repository methods.
    with tempfile.TemporaryDirectory(prefix="nutrition-reconcile-") as temporary:
        copied = backup_database(importer.database.path, Path(temporary) / "preview.sqlite3")
        repository = NutritionRepository(copied, clock=lambda: now)
        simulated = GarminImporter(repository, importer.provider)
        with repository.database.connection() as connection:
            if simulated.snapshot(connection) != snapshot:
                raise ValueError("local state changed during preview; regenerate")
            goals_snapshot = [
                dict(r) for r in connection.execute("SELECT * FROM daily_goals ORDER BY goal_id")
            ]
            entry_snapshot = [
                dict(r)
                for r in connection.execute(
                    "SELECT entry_id,revision,deleted_at FROM entries ORDER BY entry_id"
                )
            ]
            day_snapshot = [
                dict(r)
                for r in connection.execute("SELECT * FROM day_reviews ORDER BY timezone,on_date")
            ]
        before = load_energy_balances(repository.database, lower, upper, "Europe/Zurich")
        with repository.database.connection(write=True) as connection:
            changed = simulated.apply_decisions(connection, decisions)
        after = load_energy_balances(repository.database, lower, upper, "Europe/Zurich")
    plan = {
        "plan_id": new_id(),
        "account_id": importer.account_id,
        "decisions": decisions,
        "before_after_trainings": changed,
        "ledger_differences": {
            day: {"before": before[day], "after": after[day]}
            for day in before
            if before[day] != after[day]
        },
        "goals_snapshot": goals_snapshot,
        "entry_snapshot": entry_snapshot,
        "day_snapshot": day_snapshot,
        "preview_local_date": now.astimezone(ZONE).date().isoformat(),
        "created_at": timestamp(now),
    }
    with importer.database.connection(write=True) as connection:
        if importer.snapshot(connection) != snapshot:
            raise ValueError("state changed during preview; regenerate")
        connection.execute(
            "INSERT INTO reconciliation_plans VALUES (?,?,?,?,?,NULL)",
            (
                plan["plan_id"],
                importer.account_id,
                canonical_json(snapshot),
                canonical_json(plan),
                timestamp(now),
            ),
        )
    return plan


def verify_source_snapshot(importer: GarminImporter, snapshot: dict[str, Any]) -> None:
    """Re-read remote facts before applying an approval; never trust only cached hashes."""
    today = importer.database.clock().astimezone(ZONE).date()
    for stream, key in (("activity", "sources"), ("weight", "weights")):
        originals = {r["external_id"]: r for r in snapshot[key]}
        time_key = "occurred_at" if stream == "activity" else "measured_at"
        dates = [
            parse_timestamp(json.loads(r["facts_json"])[time_key]).astimezone(ZONE).date()
            for r in originals.values()
            if json.loads(r["facts_json"]).get(time_key)
        ]
        lower = min([today - timedelta(days=90), *dates])
        current: dict[str, Any] = {}
        fetch = importer.provider.activities if stream == "activity" else importer.provider.weights
        while lower <= today:
            upper = min(lower + timedelta(days=6), today)
            for raw in fetch(lower - timedelta(days=1), upper + timedelta(days=1)):
                facts = normalize(stream, raw, snapshot["validation"])
                if facts.get(time_key) and not (
                    lower <= parse_timestamp(facts[time_key]).astimezone(ZONE).date() <= upper
                ):
                    continue
                current[facts["external_id"]] = canonical_json(facts)
            lower = upper + timedelta(days=1)
        expected = {
            identity: r["facts_json"]
            for identity, r in originals.items()
            if r["status"] != "upstream_missing"
        }
        # Confirmed missing records may be explicitly excluded; all other differences invalidate.
        for identity, record in originals.items():
            if record["status"] == "upstream_missing" and identity in current:
                raise ValueError("source reappeared since preview; sync and review again")
        if current != expected:
            raise ValueError("remote source facts changed; sync and prepare a new plan")


def apply(
    importer: GarminImporter, plan_id: str, *, approved: bool, backup: Path
) -> dict[str, Any]:
    if not approved:
        raise ValueError("explicit approval of the concrete plan is required")
    with importer.database.connection() as connection:
        prior = connection.execute(
            "SELECT * FROM reconciliation_plans WHERE plan_id=? AND account_id=?",
            (plan_id, importer.account_id),
        ).fetchone()
        if prior is None:
            raise ValueError("plan not found")
        if prior["result_json"]:
            return dict(json.loads(prior["result_json"]))
        source_snapshot = json.loads(prior["snapshot_json"])
    verify_source_snapshot(importer, source_snapshot)
    backup_database(importer.database.path, backup)
    with importer.database.connection(write=True) as connection:
        row = connection.execute(
            "SELECT * FROM reconciliation_plans WHERE plan_id=? AND account_id=?",
            (plan_id, importer.account_id),
        ).fetchone()
        if row is None:
            raise ValueError("plan not found")
        if row["result_json"]:
            return dict(json.loads(row["result_json"]))
        if importer.snapshot(connection) != json.loads(row["snapshot_json"]):
            raise ValueError("local/source records changed; plan is stale")
        plan = json.loads(row["decisions_json"])
        for query, key in (
            ("SELECT * FROM daily_goals ORDER BY goal_id", "goals_snapshot"),
            (
                "SELECT entry_id,revision,deleted_at FROM entries ORDER BY entry_id",
                "entry_snapshot",
            ),
            ("SELECT * FROM day_reviews ORDER BY timezone,on_date", "day_snapshot"),
        ):
            if [dict(r) for r in connection.execute(query)] != plan[key]:
                raise ValueError("ledger facts changed since preview; plan is stale")
        if (
            importer.database.clock().astimezone(ZONE).date().isoformat()
            != plan["preview_local_date"]
        ):
            raise ValueError("local date changed; preview ledger again")
        changes = importer.apply_decisions(connection, plan["decisions"])
        result = {
            "plan_id": plan_id,
            "changes": changes,
            "resulting_snapshot": importer.snapshot(connection),
            "applied_at": timestamp(importer.database.clock()),
        }
        connection.execute(
            "UPDATE reconciliation_plans SET result_json=? WHERE plan_id=?",
            (canonical_json(result), plan_id),
        )
    return result
