"""Load energy facts in one SQLite read transaction before calculation."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .database import Database
from .energy import credited_burn
from .energy_calculation import DayFacts, GoalFacts, calculate_ledger
from .energy_response import serialize_balances
from .models import validate_timezone
from .serialization import parse_timestamp as _parse_timestamp
from .serialization import timestamp as _timestamp


def load_energy_balances(
    database: Database, start_date: date, end_date: date, timezone: str
) -> dict[str, dict[str, Any]]:
    validate_timezone(timezone)
    zone = ZoneInfo(timezone)
    today = database.clock().astimezone(zone).date()
    with database.connection() as connection:
        connection.execute("BEGIN")
        goal_rows = connection.execute(
            """
            SELECT * FROM daily_goals
            WHERE timezone = ? AND effective_from <= ?
            ORDER BY effective_from
            """,
            (timezone, (end_date + timedelta(days=3)).isoformat()),
        ).fetchall()
        earliest_goal = (
            None if not goal_rows else date.fromisoformat(goal_rows[0]["effective_from"])
        )
        scan_start = start_date if earliest_goal is None else min(start_date, earliest_goal)
        scan_end = end_date + timedelta(days=3)
        start_utc = datetime.combine(scan_start, time.min, tzinfo=zone).astimezone(UTC)
        end_utc = datetime.combine(scan_end + timedelta(days=1), time.min, tzinfo=zone).astimezone(
            UTC
        )
        entry_rows = connection.execute(
            """
            SELECT e.entry_id, e.occurred_at_utc,
                   SUM(c.calories_mkcal) AS calories_mkcal,
                   COUNT(*) AS component_count,
                   COUNT(c.calories_mkcal) AS known_component_count
            FROM entries AS e
            JOIN entry_components AS c ON c.entry_id = e.entry_id
            WHERE e.deleted_at IS NULL
              AND e.occurred_at_utc >= ? AND e.occurred_at_utc < ?
            GROUP BY e.entry_id, e.occurred_at_utc
            """,
            (_timestamp(start_utc), _timestamp(end_utc)),
        ).fetchall()
        training_rows = connection.execute(
            """
            SELECT * FROM trainings
            WHERE deleted_at IS NULL AND occurred_at_utc >= ? AND occurred_at_utc < ?
            """,
            (_timestamp(start_utc), _timestamp(end_utc)),
        ).fetchall()

        review_rows = connection.execute(
            "SELECT on_date, exceptional_activity FROM day_reviews WHERE timezone = ?",
            (timezone,),
        ).fetchall()

    facts: dict[date, DayFacts] = {}
    for row in entry_rows:
        day = _parse_timestamp(row["occurred_at_utc"]).astimezone(zone).date()
        fact = facts.setdefault(day, DayFacts())
        fact.intake_mkcal += int(row["calories_mkcal"] or 0)
        fact.intake_logged = True
        fact.intake_complete &= row["component_count"] == row["known_component_count"]
    for row in training_rows:
        day = _parse_timestamp(row["occurred_at_utc"]).astimezone(zone).date()
        fact = facts.setdefault(day, DayFacts())
        fact.reported_mkcal += int(row["calories_burned_mkcal"])
        fact.credited_mkcal += credited_burn(int(row["calories_burned_mkcal"]), row["confidence"])
        fact.duration_ms += int(row["duration_milliseconds"])
    for row in review_rows:
        facts.setdefault(date.fromisoformat(row["on_date"]), DayFacts()).planned_exceptional = bool(
            row["exceptional_activity"]
        )
    goals = {
        date.fromisoformat(row["effective_from"]): GoalFacts(
            int(row["base_burn_mkcal"]), int(row["deficit_mkcal"])
        )
        for row in goal_rows
    }
    ledger = calculate_ledger(facts, goals, start_date, end_date, today=today)
    return serialize_balances(ledger, start_date, end_date, timezone, today)
