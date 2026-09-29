"""Persisted conversational proposals; completion is the sole weight-driven goal writer."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .body import BodyRepository
from .errors import NotFoundError, RevisionConflictError
from .models import DEFAULT_TIMEZONE, GoalInput, NutritionValues, validate_timezone
from .serialization import canonical_json, new_id, parse_timestamp, timestamp

if TYPE_CHECKING:
    from .repository import NutritionRepository


class ReviewProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    outcome: Literal["change", "keep"]
    rationale: str = Field(min_length=1, max_length=500)
    measurement_start: date
    measurement_end: date
    effective_from: date | None = None
    base_burn_kcal: float | None = Field(default=None, gt=0, le=100_000)
    deficit_kcal: float | None = Field(default=None, ge=0, le=100_000)
    targets: NutritionValues | None = None
    timezone: str = DEFAULT_TIMEZONE

    @model_validator(mode="after")
    def valid(self) -> ReviewProposal:
        validate_timezone(self.timezone)
        if self.measurement_start > self.measurement_end:
            raise ValueError("invalid measurement window")
        if self.outcome == "keep" and any(
            x is not None for x in (self.base_burn_kcal, self.deficit_kcal, self.targets)
        ):
            raise ValueError("keep proposals cannot include goal changes")
        if self.outcome == "change" and (self.base_burn_kcal is None or self.deficit_kcal is None):
            raise ValueError("change proposals require explicit base burn and deficit")
        return self


class WeightReviewRepository:
    def __init__(self, repository: NutritionRepository) -> None:
        self.repository = repository
        self.database = repository.database

    def today(self, timezone: str) -> date:
        validate_timezone(timezone)
        return self.database.clock().astimezone(ZoneInfo(timezone)).date()

    @staticmethod
    def goals_snapshot(connection: sqlite3.Connection, timezone: str) -> str:
        # Include future goals and the audit count: even edit-and-revert invalidates approval.
        return canonical_json(
            {
                "goals": [
                    dict(r)
                    for r in connection.execute(
                        "SELECT * FROM daily_goals WHERE timezone=? ORDER BY effective_from",
                        (timezone,),
                    )
                ],
                "audit_count": connection.execute(
                    "SELECT COUNT(*) FROM goal_revisions r JOIN daily_goals g USING(goal_id) "
                    "WHERE g.timezone=?",
                    (timezone,),
                ).fetchone()[0],
            }
        )

    def status(self, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
        today = self.today(timezone)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM weight_review_reminders WHERE timezone=?", (timezone,)
            ).fetchone()
        state: dict[str, Any] = (
            dict(row)
            if row
            else {
                "timezone": timezone,
                "revision": 0,
                "enabled": False,
                "next_due": None,
                "last_completed_at": None,
                "last_hinted_at": None,
                "snoozed_until": None,
            }
        )
        due = bool(state["enabled"] and state["next_due"] <= today.isoformat())
        eligible = due
        if state["snoozed_until"]:
            eligible = bool(state["enabled"] and state["snoozed_until"] <= today.isoformat())
        elif state["last_hinted_at"]:
            hinted = parse_timestamp(state["last_hinted_at"]).astimezone(ZoneInfo(timezone)).date()
            eligible = due and today >= hinted + timedelta(days=30)
        latest = BodyRepository(self.database).latest()
        return {
            **state,
            "enabled": bool(state["enabled"]),
            "due": due,
            "hint_eligible": eligible,
            "weight_stale": latest["stale"],
            "latest_measured_at": (latest["measurement"] or {}).get("measured_at"),
        }

    def get(self, timezone: str = DEFAULT_TIMEZONE) -> dict[str, Any]:
        today = self.today(timezone)
        with self.database.connection() as connection:
            proposals = [
                json.loads(r[0])
                for r in connection.execute(
                    "SELECT proposal_json FROM weight_budget_reviews WHERE timezone=? "
                    "AND result_json IS NULL ORDER BY created_at DESC LIMIT 10",
                    (timezone,),
                )
            ]
        from datetime import datetime, time

        return {
            "reminder": self.status(timezone),
            "weights": BodyRepository(self.database).list_measurements(
                start=datetime.combine(today - timedelta(days=90), time.min, ZoneInfo(timezone)),
                limit=100,
            ),
            "goals": self.repository.get_goals(timezone=timezone),
            "proposals": proposals,
            "guidance": "Report available data and gaps; no automatic calorie recommendation.",
        }

    def update_reminder(
        self,
        action: Literal["acknowledge", "snooze", "enable", "disable"],
        expected_revision: int,
        timezone: str = DEFAULT_TIMEZONE,
        until: date | None = None,
    ) -> dict[str, Any]:
        today = self.today(timezone)
        if action not in ("acknowledge", "snooze", "enable", "disable"):
            raise ValueError("unknown reminder action")
        if until is not None and (action != "snooze" or until <= today):
            raise ValueError("snooze must end on a future date")
        with self.database.connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM weight_review_reminders WHERE timezone=?", (timezone,)
            ).fetchone()
            actual = row["revision"] if row else 0
            if actual != expected_revision:
                raise RevisionConflictError(
                    timezone, expected_revision, actual, record_type="weight_review_reminder"
                )
            if row is None:
                connection.execute(
                    "INSERT INTO weight_review_reminders(timezone,revision,enabled,next_due) "
                    "VALUES (?,0,0,?)",
                    (timezone, (today + timedelta(days=30)).isoformat()),
                )
            if action == "enable":
                connection.execute(
                    "UPDATE weight_review_reminders SET enabled=1, next_due=?, "
                    "snoozed_until=NULL,last_hinted_at=NULL WHERE timezone=? AND enabled=0",
                    ((today + timedelta(days=30)).isoformat(), timezone),
                )
            elif action == "disable":
                connection.execute(
                    "UPDATE weight_review_reminders SET enabled=0 WHERE timezone=?", (timezone,)
                )
            elif action == "acknowledge":
                connection.execute(
                    "UPDATE weight_review_reminders SET last_hinted_at=?,snoozed_until=NULL "
                    "WHERE timezone=?",
                    (timestamp(self.database.clock()), timezone),
                )
            else:
                connection.execute(
                    "UPDATE weight_review_reminders SET snoozed_until=? WHERE timezone=?",
                    ((until or today + timedelta(days=7)).isoformat(), timezone),
                )
            connection.execute(
                "UPDATE weight_review_reminders SET revision=revision+1 WHERE timezone=?",
                (timezone,),
            )
        return self.status(timezone)

    def propose(self, request: ReviewProposal) -> dict[str, Any]:
        today = self.today(request.timezone)
        effective = request.effective_from or today + timedelta(days=1)
        with self.database.connection(write=True) as connection:
            snapshot = self.goals_snapshot(connection, request.timezone)
            rows = json.loads(snapshot)["goals"]
            applicable = [r for r in rows if r["effective_from"] <= effective.isoformat()]
            if not applicable:
                raise ValueError("set initial goals explicitly before a weight-budget review")
            current = self.repository._goal_from_row(applicable[-1])
            targets = dict(current["targets"])
            if request.targets is not None:
                targets.update(request.targets.model_dump(exclude_unset=True))
            if request.outcome == "change":
                assert request.base_burn_kcal is not None
                assert request.deficit_kcal is not None
                goal = GoalInput(
                    effective_from=effective,
                    timezone=request.timezone,
                    base_burn_kcal=request.base_burn_kcal,
                    deficit_kcal=request.deficit_kcal,
                    targets=NutritionValues.model_validate(targets),
                    reason=request.rationale,
                )
            else:
                goal = GoalInput(
                    effective_from=effective,
                    timezone=request.timezone,
                    base_burn_kcal=current["base_burn_kcal"],
                    deficit_kcal=current["deficit_kcal"],
                    targets=NutritionValues.model_validate(targets),
                    reason=request.rationale,
                )
            proposal = {
                **request.model_dump(mode="json"),
                "proposal_id": new_id(),
                "effective_from": effective.isoformat(),
                "current_goals": current,
                "proposed_goals": goal.model_dump(mode="json"),
                "ordinary_target_kcal": float(goal.base_burn_kcal - goal.deficit_kcal),
                "backdated": effective < today,
                "historical_effect": "Recalculates this date and subsequent energy balances."
                if effective < today
                else None,
            }
            connection.execute(
                "INSERT INTO weight_budget_reviews VALUES (?,?,?,?,?,NULL)",
                (
                    proposal["proposal_id"],
                    request.timezone,
                    canonical_json(proposal),
                    snapshot,
                    timestamp(self.database.clock()),
                ),
            )
        return proposal

    def complete(
        self,
        proposal_id: str,
        *,
        user_approved: bool,
        approved_backdate: date | None = None,
    ) -> dict[str, Any]:
        if not user_approved:
            raise ValueError("explicit conversational approval is required")
        with self.database.connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM weight_budget_reviews WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("review proposal not found")
            if row["result_json"]:
                return dict(json.loads(row["result_json"]))
            proposal = json.loads(row["proposal_json"])
            timezone = row["timezone"]
            today = self.today(timezone)
            effective = date.fromisoformat(proposal["effective_from"])
            if (
                proposal["outcome"] == "change"
                and effective < today
                and approved_backdate != effective
            ):
                raise ValueError(
                    "explicit approval of backdated date and historical effect required"
                )
            if self.goals_snapshot(connection, timezone) != row["goals_snapshot_json"]:
                raise ValueError("stale proposal: saved goals changed; propose again")
            goal = None
            if proposal["outcome"] == "change":
                goal = self.repository.set_goals_in_transaction(
                    connection, GoalInput.model_validate(proposal["proposed_goals"])
                )
            now = timestamp(self.database.clock())
            result = {
                "proposal_id": proposal_id,
                "outcome": proposal["outcome"],
                "completed_at": now,
                "goal": goal,
            }
            connection.execute(
                "UPDATE weight_budget_reviews SET result_json=? WHERE proposal_id=?",
                (canonical_json(result), proposal_id),
            )
            connection.execute(
                "INSERT INTO weight_review_reminders(timezone,revision,enabled,next_due,"
                "last_completed_at) VALUES (?,1,0,?,?) ON CONFLICT(timezone) DO UPDATE SET "
                "revision=revision+1,next_due=excluded.next_due,last_completed_at=excluded."
                "last_completed_at,last_hinted_at=NULL,snoozed_until=NULL",
                (timezone, (today + timedelta(days=30)).isoformat(), now),
            )
        return result
