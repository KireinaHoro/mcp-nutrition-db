"""Transactional imports, explicit historical decisions, and activation gates."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .garmin import GarminProvider, normalize, source_hash
from .models import LogTrainingInput, TrainingChanges
from .repository import NutritionRepository
from .serialization import canonical_json, new_id, parse_timestamp, timestamp

ZONE = ZoneInfo("Europe/Zurich")
OWNED = ("occurred_at", "timezone", "duration_minutes", "reported_burn_kcal")


class GarminImporter:
    def __init__(self, repository: NutritionRepository, provider: GarminProvider) -> None:
        self.repository = repository
        self.database = repository.database
        self.provider = provider
        self.account_id = provider.account_id

    def register(self) -> None:
        with self.database.connection(write=True) as connection:
            other = connection.execute(
                "SELECT account_id FROM garmin_accounts WHERE account_id != ?", (self.account_id,)
            ).fetchone()
            if other:
                raise ValueError("this database is already connected to another Garmin account")
            connection.execute(
                "INSERT INTO garmin_accounts(account_id,auth_state) VALUES (?,'ready') "
                "ON CONFLICT(account_id) DO NOTHING",
                (self.account_id,),
            )

    def validation(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT validation_json FROM garmin_accounts WHERE account_id=?", (self.account_id,)
            ).fetchone()
        return json.loads(row[0]) if row else {}

    def validate(self, validation: dict[str, Any]) -> None:
        with self.database.connection(write=True) as connection:
            if connection.execute(
                "SELECT activated_at FROM garmin_accounts WHERE account_id=?", (self.account_id,)
            ).fetchone()[0]:
                raise ValueError(
                    "cannot change mapping after activation; reconcile through a new reviewed "
                    "mapping"
                )
            connection.execute(
                "UPDATE garmin_accounts SET validation_json=? WHERE account_id=?",
                (canonical_json(validation), self.account_id),
            )

    def ranges(self, stream: str, *, weekly: bool = False) -> list[tuple[date, date]]:
        today = self.database.clock().astimezone(ZONE).date()
        with self.database.connection() as connection:
            first = connection.execute("SELECT MIN(occurred_at_utc) FROM trainings").fetchone()[0]
            coverage = connection.execute(
                "SELECT start_date,end_date FROM sync_coverage WHERE account_id=? AND stream=? "
                "ORDER BY start_date",
                (self.account_id, stream),
            ).fetchall()
        earliest = today - timedelta(days=90)
        if stream == "activity" and first:
            earliest = parse_timestamp(first).astimezone(ZONE).date() - timedelta(days=1)
        # Resume from first hole, not MAX(end_date), which would hide incomplete backfills.
        frontier = earliest
        for row in coverage:
            start, end = date.fromisoformat(row[0]), date.fromisoformat(row[1])
            if start > frontier:
                break
            if end >= frontier:
                frontier = end + timedelta(days=1)
        recent = max(earliest, today - timedelta(days=90 if weekly else 7))
        intervals = [(recent, today)]
        if frontier < recent:
            intervals.append((frontier, recent - timedelta(days=1)))
        chunks = []
        for start, end in intervals:
            while start <= end:
                stop = min(start + timedelta(days=6), end)
                chunks.append((start, stop))
                start = stop + timedelta(days=1)
        return chunks

    def sync(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        dry_run: bool = False,
        weekly: bool = False,
    ) -> dict[str, Any]:
        if (start is None) != (end is None) or (start and end and start > end):
            raise ValueError("supply a valid inclusive start/end range")
        validation = self.validation()
        report: dict[str, Any] = {"dry_run": dry_run, "chunks": [], "counts": {}}
        for stream in ("weight", "activity"):
            if start and end:
                ranges = []
                current = start
                while current <= end:
                    stop = min(current + timedelta(days=6), end)
                    ranges.append((current, stop))
                    current = stop + timedelta(days=1)
            else:
                ranges = self.ranges(stream, weekly=weekly)
            for lower, upper in ranges[:60]:
                # All pages and detail requests finish outside a write transaction.
                raw = (self.provider.weights if stream == "weight" else self.provider.activities)(
                    lower - timedelta(days=1), upper + timedelta(days=1)
                )
                records = [normalize(stream, value, validation) for value in raw]
                time_key = "measured_at" if stream == "weight" else "occurred_at"
                records = [
                    r
                    for r in records
                    if not r.get(time_key)
                    or lower <= parse_timestamp(r[time_key]).astimezone(ZONE).date() <= upper
                ]
                ids = [r["external_id"] for r in records]
                if len(set(ids)) != len(ids):
                    raise ValueError("duplicate external IDs in a complete chunk")
                counts: dict[str, int] = {}
                with self.database.connection(write=not dry_run) as connection:
                    for facts in records:
                        if dry_run:
                            status = "pending" if facts["issues"] else "validated"
                        else:
                            status = self._store(connection, stream, facts)
                        counts[status] = counts.get(status, 0) + 1
                    if not dry_run:
                        self._missing(connection, stream, lower, upper, set(ids))
                        connection.execute(
                            "INSERT INTO sync_coverage VALUES (?,?,?,?,?) "
                            "ON CONFLICT(account_id,stream,start_date,end_date) DO UPDATE SET "
                            "completed_at=excluded.completed_at",
                            (
                                self.account_id,
                                stream,
                                lower.isoformat(),
                                upper.isoformat(),
                                timestamp(self.database.clock()),
                            ),
                        )
                report["chunks"].append(
                    {
                        "stream": stream,
                        "start": lower.isoformat(),
                        "end": upper.isoformat(),
                        "counts": counts,
                    }
                )
        return report

    def _stage(
        self, connection: sqlite3.Connection, stream: str, facts: dict[str, Any], status: str
    ) -> None:
        connection.execute(
            "INSERT INTO import_staging VALUES (?,?,?,?,?,?,?) ON "
            "CONFLICT(account_id,stream,external_id) "
            "DO UPDATE SET facts_json=excluded.facts_json,source_hash=excluded.source_hash,"
            "status=excluded.status,fetched_at=excluded.fetched_at",
            (
                self.account_id,
                stream,
                facts["external_id"],
                canonical_json(facts),
                source_hash(facts),
                status,
                timestamp(self.database.clock()),
            ),
        )

    def _store(self, connection: sqlite3.Connection, stream: str, facts: dict[str, Any]) -> str:
        previous = connection.execute(
            "SELECT status FROM import_staging WHERE account_id=? AND stream=? AND external_id=?",
            (self.account_id, stream, facts["external_id"]),
        ).fetchone()
        if previous and previous[0] in ("excluded", "suppressed"):
            self._stage(connection, stream, facts, previous[0])
            return str(previous[0])
        if facts["issues"]:
            self._stage(connection, stream, facts, "pending")
            return "pending"
        if stream == "weight":
            status = self._weight(connection, facts)
        else:
            status = self._activity(connection, facts)
        self._stage(connection, stream, facts, status)
        return status

    def _weight(self, connection: sqlite3.Connection, facts: dict[str, Any]) -> str:
        prior = connection.execute(
            "SELECT * FROM body_measurements WHERE account_id=? AND external_id=?",
            (self.account_id, facts["external_id"]),
        ).fetchone()
        now = timestamp(self.database.clock())
        if prior:
            if prior["deleted_at"]:
                return "suppressed"
            if prior["facts_json"] == canonical_json(facts):
                return "imported"
            connection.execute(
                "INSERT INTO body_measurement_revisions VALUES (?,?,?,?,?)",
                (
                    prior["measurement_id"],
                    prior["revision"],
                    canonical_json(dict(prior)),
                    "Garmin measurement correction",
                    now,
                ),
            )
            connection.execute(
                "UPDATE body_measurements SET measured_at=?,weight_grams=?,source=?,facts_json=?,"
                "revision=revision+1,fetched_at=? WHERE measurement_id=?",
                (
                    facts["measured_at"],
                    facts["weight_grams"],
                    facts["source"],
                    canonical_json(facts),
                    now,
                    prior["measurement_id"],
                ),
            )
        else:
            connection.execute(
                "INSERT INTO body_measurements VALUES (?,?,?,?,?,?,?,1,?,NULL)",
                (
                    new_id(),
                    self.account_id,
                    facts["external_id"],
                    facts["measured_at"],
                    facts["weight_grams"],
                    facts["source"],
                    canonical_json(facts),
                    now,
                ),
            )
        return "imported"

    @staticmethod
    def owned(facts: dict[str, Any]) -> dict[str, Any]:
        return {
            "occurred_at": parse_timestamp(facts["occurred_at"]).astimezone(ZONE).isoformat(),
            "timezone": "Europe/Zurich",
            "duration_minutes": facts["duration_ms"] / 60_000,
            "reported_burn_kcal": facts["active_mkcal"] / 1000,
        }

    def candidates(
        self, connection: sqlite3.Connection, facts: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not facts.get("occurred_at"):
            return []
        day = parse_timestamp(facts["occurred_at"]).astimezone(ZONE).date()
        return [
            dict(r)
            for r in connection.execute(
                "SELECT t.* FROM trainings t LEFT JOIN external_records e USING(training_id) "
                "WHERE e.training_id IS NULL"
            )
            if parse_timestamp(r["occurred_at_utc"]).astimezone(ZONE).date() == day
        ]

    def _activity(self, connection: sqlite3.Connection, facts: dict[str, Any]) -> str:
        mapping = connection.execute(
            "SELECT * FROM external_records WHERE account_id=? AND external_id=?",
            (self.account_id, facts["external_id"]),
        ).fetchone()
        if mapping:
            if mapping["suppressed"] or not mapping["training_id"]:
                return "suppressed" if mapping["training_id"] else "excluded"
            row = connection.execute(
                "SELECT * FROM trainings WHERE training_id=?", (mapping["training_id"],)
            ).fetchone()
            if row["deleted_at"]:
                connection.execute(
                    "UPDATE external_records SET suppressed=1 WHERE account_id=? AND external_id=?",
                    (self.account_id, facts["external_id"]),
                )
                return "suppressed"
            if mapping["source_hash"] == source_hash(facts):
                return "imported"
            current = self.repository._training_from_row(row)
            baseline = json.loads(mapping["owned_json"])
            proposed = self.owned(facts)
            if any(current[k] != baseline[k] and proposed[k] != current[k] for k in OWNED):
                return "local_override_conflict"
            self._apply(connection, facts, current["training_id"])
            return "imported"
        activated = connection.execute(
            "SELECT activated_at FROM garmin_accounts WHERE account_id=?", (self.account_id,)
        ).fetchone()[0]
        if not activated or facts["occurred_at"] <= activated:
            return "historical_review"
        if self.candidates(connection, facts):
            return "possible_manual_duplicate"
        self._apply(connection, facts, None)
        return "imported"

    def _apply(
        self, connection: sqlite3.Connection, facts: dict[str, Any], training_id: str | None
    ) -> dict[str, Any]:
        owned = self.owned(facts)
        if training_id:
            current = self.repository._get_training(connection, training_id)
            if any(current[k] != owned[k] for k in OWNED):
                training = self.repository.update_training_in_transaction(
                    connection,
                    training_id,
                    current["revision"],
                    "Approved Garmin source facts",
                    TrainingChanges.model_validate(owned),
                )
            else:
                training = current
        else:
            training = self.repository.create_training_in_transaction(
                connection,
                LogTrainingInput.model_validate(
                    {
                        **owned,
                        "activity": facts["activity"],
                        "confidence": facts["confidence"],
                        "measurement_method": facts["measurement_method"],
                        "source": {
                            "type": "wearable",
                            "detail": "Garmin Connect recorded activity",
                        },
                        "force_new": True,
                    }
                ),
            )
            # Imports have a creation audit in addition to their permanent source identity.
            connection.execute(
                "INSERT INTO training_revisions VALUES (?,?,1,'create',?,?,?)",
                (
                    new_id(),
                    training["training_id"],
                    "Garmin import",
                    canonical_json(training),
                    timestamp(self.database.clock()),
                ),
            )
        connection.execute(
            "INSERT INTO external_records VALUES (?,?,?,?,?,0) ON CONFLICT(account_id,external_id) "
            "DO UPDATE SET source_hash=excluded.source_hash,owned_json=excluded.owned_json,"
            "training_id=excluded.training_id,suppressed=0",
            (
                self.account_id,
                facts["external_id"],
                training["training_id"],
                source_hash(facts),
                canonical_json(owned),
            ),
        )
        return training

    def _missing(
        self, connection: sqlite3.Connection, stream: str, start: date, end: date, seen: set[str]
    ) -> None:
        for row in connection.execute(
            "SELECT * FROM import_staging WHERE account_id=? AND stream=?",
            (self.account_id, stream),
        ).fetchall():
            facts = json.loads(row["facts_json"])
            instant = facts.get("measured_at" if stream == "weight" else "occurred_at")
            if not instant:
                continue
            day = parse_timestamp(instant).astimezone(ZONE).date()
            if (
                start <= day <= end
                and row["external_id"] not in seen
                and row["status"] not in ("suppressed", "excluded")
            ):
                # Fetches overlap both boundaries by one day before filtering to accounting dates.
                connection.execute(
                    "UPDATE import_staging SET status='upstream_missing' "
                    "WHERE account_id=? AND stream=? AND external_id=?",
                    (self.account_id, stream, row["external_id"]),
                )

    def snapshot(self, connection: sqlite3.Connection) -> dict[str, Any]:
        return {
            "trainings": [
                dict(r) for r in connection.execute("SELECT * FROM trainings ORDER BY training_id")
            ],
            "sources": [
                {k: r[k] for k in ("external_id", "source_hash", "facts_json", "status")}
                for r in connection.execute(
                    "SELECT * FROM import_staging WHERE account_id=? "
                    "AND stream='activity' ORDER BY external_id",
                    (self.account_id,),
                )
            ],
            "mappings": [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM external_records ORDER BY account_id,external_id"
                )
            ],
            "weights": [
                {k: r[k] for k in ("external_id", "facts_json", "status")}
                for r in connection.execute(
                    "SELECT * FROM import_staging WHERE account_id=? AND stream='weight' "
                    "ORDER BY external_id",
                    (self.account_id,),
                )
            ],
            "measurements": [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM body_measurements ORDER BY measurement_id"
                )
            ],
            "coverage": [
                dict(r)
                for r in connection.execute(
                    "SELECT stream,start_date,end_date FROM sync_coverage WHERE account_id=? "
                    "ORDER BY stream,start_date,end_date",
                    (self.account_id,),
                )
            ],
            "validation": self.validation(),
        }

    def report(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            snapshot = self.snapshot(connection)
            proposals = []
            for source in snapshot["sources"]:
                facts = json.loads(source["facts_json"])
                mapping = next(
                    (
                        m
                        for m in snapshot["mappings"]
                        if m["account_id"] == self.account_id
                        and m["external_id"] == source["external_id"]
                    ),
                    None,
                )
                proposals.append(
                    {
                        "external_id": source["external_id"],
                        "status": source["status"],
                        "facts": facts,
                        "proposed_owned": self.owned(facts) if not facts["issues"] else None,
                        "candidates": self.candidates(connection, facts),
                        "mapping": mapping,
                        "action": "unresolved",
                        "training_id": None,
                    }
                )
        return {
            "account_id": self.account_id,
            "snapshot": snapshot,
            "activities": proposals,
            "weight_decisions": {
                w["external_id"]: "preserve"
                if w["status"] in ("imported", "excluded", "suppressed")
                else "unresolved"
                for w in snapshot["weights"]
            },
            "local_decisions": {t["training_id"]: "unresolved" for t in snapshot["trainings"]},
            "instructions": "Select add/link/exclude for each unresolved source, and preserve/link "
            "for every local training. No candidate constitutes an identity match. Prepare a plan "
            "to calculate exact ledger differences, then approve that plan before applying.",
        }

    def apply_decisions(
        self, connection: sqlite3.Connection, decisions: dict[str, Any]
    ) -> list[dict[str, Any]]:
        selected = {i["external_id"] for i in decisions["activities"] if i["action"] != "exclude"}
        for item in decisions["activities"]:
            raw = item["facts"]["raw"]
            relatives = {str(raw.get("parentId")), str(raw.get("parentActivityId"))}
            relatives.update(str(v) for v in (raw.get("childIds") or []))
            if item["external_id"] in selected and relatives & selected:
                raise ValueError("select either multisport parent or children, never both")
        for identity, action in decisions.get("weight_decisions", {}).items():
            if action == "preserve":
                continue
            if action != "exclude":
                raise ValueError("resolve pending weights with exclude or validated sync")
            row = connection.execute(
                "SELECT * FROM body_measurements WHERE account_id=? AND external_id=?",
                (self.account_id, identity),
            ).fetchone()
            if row and not row["deleted_at"]:
                now = timestamp(self.database.clock())
                connection.execute(
                    "INSERT INTO body_measurement_revisions VALUES (?,?,?,?,?)",
                    (
                        row["measurement_id"],
                        row["revision"],
                        canonical_json(dict(row)),
                        "Approved reconciliation exclusion",
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE body_measurements SET deleted_at=?,revision=revision+1 "
                    "WHERE measurement_id=?",
                    (now, row["measurement_id"]),
                )
            connection.execute(
                "UPDATE import_staging SET status='excluded' WHERE account_id=? "
                "AND stream='weight' AND external_id=?",
                (self.account_id, identity),
            )
        linked: set[str] = set()
        results = []
        for item in decisions["activities"]:
            facts = item["facts"]
            identity = item["external_id"]
            source = connection.execute(
                "SELECT * FROM import_staging WHERE account_id=? AND stream='activity' AND "
                "external_id=?",
                (self.account_id, identity),
            ).fetchone()
            if source is None or source["facts_json"] != canonical_json(facts):
                raise ValueError("source facts changed; regenerate report")
            action = item["action"]
            if action == "exclude":
                existing = connection.execute(
                    "SELECT training_id FROM external_records WHERE account_id=? AND external_id=?",
                    (self.account_id, identity),
                ).fetchone()
                if existing and existing[0]:
                    # Exclusion freezes the entry; deleting credit needs an audited correction.
                    connection.execute(
                        "UPDATE external_records SET suppressed=1 WHERE account_id=? AND "
                        "external_id=?",
                        (self.account_id, identity),
                    )
                else:
                    connection.execute(
                        "INSERT INTO external_records VALUES (?,?,NULL,?,'{}',1) "
                        "ON CONFLICT(account_id,external_id) DO UPDATE SET suppressed=1",
                        (self.account_id, identity, source["source_hash"]),
                    )
                self._stage(connection, "activity", facts, "excluded")
                continue
            if action not in ("add", "link"):
                raise ValueError("resolve every source with add, link, or exclude")
            issues = set(facts["issues"])
            if item.get("multisport_selected") is True:
                issues.discard("multisport_selection_required")
            if issues or source["status"] == "upstream_missing":
                raise ValueError("unverified or missing source cannot be imported")
            existing_mapping = connection.execute(
                "SELECT training_id FROM external_records WHERE account_id=? AND external_id=?",
                (self.account_id, identity),
            ).fetchone()
            if (
                existing_mapping
                and existing_mapping[0]
                and (action != "link" or item.get("training_id") != existing_mapping[0])
            ):
                raise ValueError("existing source identity must retain its reviewed local link")
            training_id = item.get("training_id") if action == "link" else None
            if action == "link":
                if not training_id or training_id in linked:
                    raise ValueError("each link needs a distinct local training")
                linked.add(training_id)
                if decisions["local_decisions"].get(training_id) != "link":
                    raise ValueError("local link decision missing")
            result = self._apply(connection, facts, training_id)
            results.append(result)
            self._stage(connection, "activity", facts, "imported")
        for identity, action in decisions["local_decisions"].items():
            if action != ("link" if identity in linked else "preserve"):
                raise ValueError("every local training requires link or preserve")
        return results

    def activate(self) -> dict[str, Any]:
        validation = self.validation()
        if not all(
            validation.get(k)
            for k in (
                "login_validated",
                "session_restart_validated",
                "weight_grams_epoch_ms",
                "activity_ids_utc_seconds",
            )
        ):
            raise ValueError("complete live mapping and session validation before activation")
        if not (
            validation.get("explicit_active_calories") or validation.get("total_minus_resting")
        ):
            raise ValueError("active calorie mapping must be verified")
        with self.database.connection(write=True) as connection:
            row = connection.execute(
                "SELECT activated_at FROM garmin_accounts WHERE account_id=?", (self.account_id,)
            ).fetchone()
            if row[0]:
                return {"activated_at": row[0]}
            pending = connection.execute(
                "SELECT COUNT(*) FROM import_staging WHERE account_id=? "
                "AND status NOT IN ('imported','excluded','suppressed')",
                (self.account_id,),
            ).fetchone()[0]
            if pending:
                raise ValueError("resolve or exclude all pending records before activation")
            plan = connection.execute(
                "SELECT result_json FROM reconciliation_plans WHERE account_id=? "
                "AND result_json IS NOT NULL ORDER BY json_extract(result_json, '$.applied_at') "
                "DESC, rowid DESC LIMIT 1",
                (self.account_id,),
            ).fetchone()
            if plan is None:
                raise ValueError("apply an explicitly approved full-history reconciliation first")
            applied = json.loads(plan[0])
            if self.snapshot(connection) != applied["resulting_snapshot"]:
                raise ValueError("history changed since reconciliation; reconcile again")
            today = self.database.clock().astimezone(ZONE).date()
            first = connection.execute("SELECT MIN(occurred_at_utc) FROM trainings").fetchone()[0]
            for stream in ("weight", "activity"):
                frontier = today - timedelta(days=90)
                if stream == "activity" and first:
                    frontier = parse_timestamp(first).astimezone(ZONE).date() - timedelta(days=1)
                for coverage in connection.execute(
                    "SELECT start_date,end_date FROM sync_coverage WHERE account_id=? "
                    "AND stream=? ORDER BY start_date",
                    (self.account_id, stream),
                ):
                    lower, upper = date.fromisoformat(coverage[0]), date.fromisoformat(coverage[1])
                    if lower > frontier:
                        break
                    if upper >= frontier:
                        frontier = upper + timedelta(days=1)
                if frontier <= today:
                    raise ValueError("historical sync coverage incomplete")
            now = timestamp(self.database.clock())
            connection.execute(
                "UPDATE garmin_accounts SET activated_at=? WHERE account_id=?",
                (now, self.account_id),
            )
            due = (self.database.clock().astimezone(ZONE).date() + timedelta(days=30)).isoformat()
            connection.execute(
                "INSERT INTO weight_review_reminders(timezone,revision,enabled,next_due) "
                "VALUES ('Europe/Zurich',1,1,?) ON CONFLICT(timezone) DO UPDATE SET "
                "enabled=1,revision=revision+1,next_due=excluded.next_due",
                (due,),
            )
        return {"activated_at": now}
