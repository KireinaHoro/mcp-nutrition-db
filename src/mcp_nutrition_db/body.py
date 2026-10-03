"""Read-only body history and importer health. These paths cannot mutate goals."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any

from .database import Database
from .serialization import parse_timestamp, timestamp


class BodyRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def sync_status(self) -> dict[str, Any]:
        now = self.database.clock()
        with self.database.connection() as connection:
            accounts = [
                dict(r)
                for r in connection.execute(
                    "SELECT account_id,auth_state,activated_at,last_error FROM garmin_accounts"
                )
            ]
            coverage = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM sync_coverage ORDER BY account_id,stream,start_date"
                )
            ]
            pending = [
                dict(r)
                for r in connection.execute(
                    "SELECT account_id,stream,status,COUNT(*) AS count FROM import_staging "
                    "WHERE status NOT IN ('imported','excluded','suppressed') "
                    "GROUP BY account_id,stream,status"
                )
            ]
        streams: dict[str, Any] = {}
        for stream in ("weight", "activity"):
            ranges = [r for r in coverage if r["stream"] == stream]
            latest = max((r["completed_at"] for r in ranges), default=None)
            streams[stream] = {
                "last_success": latest,
                "stale": latest is None or now - parse_timestamp(latest) > timedelta(hours=2),
                "coverage": ranges,
            }
        disconnected = any(a["auth_state"] == "reauth_required" for a in accounts)
        stalled = bool(accounts) and (
            any(s["stale"] for s in streams.values()) or any(a["last_error"] for a in accounts)
        )
        return {
            "accounts": accounts,
            "streams": streams,
            "pending": pending,
            "connection_hint": {
                "eligible": disconnected or stalled,
                "state": "reauth_required" if disconnected else "sync_stale" if stalled else "ok",
                "message": (
                    "Garmin authentication needs attention; automatic retries continue. "
                    "If it persists, generate a new login session and replace the sops "
                    "secret."
                    if disconnected
                    else "Garmin sync is delayed; weight and activity data may be incomplete."
                    if stalled
                    else None
                ),
            },
        }

    def list_measurements(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        clauses = ["deleted_at IS NULL"]
        values: list[Any] = []
        for name, value, operator in (("start", start, ">="), ("end", end, "<=")):
            if value is not None:
                if value.tzinfo is None:
                    raise ValueError(f"{name} must have a timezone")
                clauses.append(f"measured_at {operator} ?")
                values.append(timestamp(value))
        if start and end and start > end:
            raise ValueError("start must be before end")
        if cursor:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor))
                if position["start"] != (timestamp(start) if start else None) or position[
                    "end"
                ] != (timestamp(end) if end else None):
                    raise ValueError("cursor window mismatch")
                clauses.append("(measured_at,measurement_id) < (?,?)")
                values.extend([position["at"], position["id"]])
            except (KeyError, ValueError, TypeError):
                raise ValueError("invalid measurement cursor") from None
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM body_measurements WHERE "
                + " AND ".join(clauses)
                + " ORDER BY measured_at DESC,measurement_id DESC LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
        records = [
            {
                "measurement_id": r["measurement_id"],
                "measured_at": r["measured_at"],
                "weight_kg": r["weight_grams"] / 1000,
                "source": r["source"],
                "revision": r["revision"],
                "fetched_at": r["fetched_at"],
            }
            for r in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "at": records[-1]["measured_at"],
                        "id": records[-1]["measurement_id"],
                        "start": timestamp(start) if start else None,
                        "end": timestamp(end) if end else None,
                    }
                ).encode()
            ).decode()
        return {"measurements": records, "next_cursor": next_cursor}

    def latest(self, as_of: datetime | None = None) -> dict[str, Any]:
        now = self.database.clock()
        records = self.list_measurements(end=as_of or now, limit=1)["measurements"]
        weight = records[0] if records else None
        age = (now - parse_timestamp(weight["measured_at"])).total_seconds() if weight else None
        return {
            "measurement": weight,
            "age_seconds": age,
            "stale": age is None or age > 7 * 86400,
            "sync": self.sync_status(),
        }
