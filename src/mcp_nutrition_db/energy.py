"""Active energy-policy parameters and read-only travel inference."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

POLICY_ID = "energy-credit/v3"
DEBIT_EFFECTIVE_FROM = date(2026, 9, 9)
DAILY_ADJUSTMENT_MKCAL = 200_000
EXCEPTIONAL_BURN_MKCAL = 1_000_000
EXCEPTIONAL_DURATION_MS = 180 * 60_000
REVIEW_AFTER_ELIGIBLE_DAYS = 28
CONTEXT_SINGLE_SURPLUS_MKCAL = 500_000
CONTEXT_REPEATED_SURPLUS_MKCAL = 250_000
CONTEXT_REPEAT_WINDOW_DAYS = 3


def travel_context(
    on_date: date, trips: list[dict[str, Any]], signals: list[dict[str, Any]]
) -> dict[str, Any]:
    """Infer a prompt, never a stored trip or a return date, from logged evidence."""
    for trip in reversed(trips):
        if trip["status"] != "active" or trip["start_date"] > on_date.isoformat():
            continue
        returned = trip["return_date"]
        if returned is None or returned >= on_date.isoformat():
            return {
                "status": "confirmed",
                "trip": trip,
                "adjustment_paused": True,
                "action_required": "ask_return_date" if returned is None else None,
                "recovery_start_date": (
                    None
                    if returned is None
                    else (date.fromisoformat(returned) + timedelta(days=1)).isoformat()
                ),
            }

    unresolved = []
    for signal in signals:
        signal_date = signal["date"]
        if signal_date > on_date.isoformat():
            continue
        covered = any(
            trip["start_date"] <= signal_date
            and (trip["return_date"] is None or signal_date <= trip["return_date"])
            for trip in trips
        )
        if not covered:
            unresolved.append(signal)
    triggers = []
    for index, signal in enumerate(unresolved):
        neighbours = [
            other
            for other in unresolved[:index]
            if (date.fromisoformat(signal["date"]) - date.fromisoformat(other["date"])).days
            < CONTEXT_REPEAT_WINDOW_DAYS
        ]
        if signal["surplus_kcal"] >= CONTEXT_SINGLE_SURPLUS_MKCAL / 1000:
            triggers.append(signal)
        elif neighbours:
            triggers.extend([*neighbours, signal])
    if triggers:
        evidence = {signal["date"]: signal for signal in triggers}
        ordered = [evidence[key] for key in sorted(evidence)]
        return {
            "status": "needs_context",
            "suggested_start_date": ordered[0]["date"],
            "evidence": ordered[-10:],
            "adjustment_paused": True,
            "action_required": "ask_overshoot_context",
            "question": (
                "Is this surplus related to an ongoing trip or a one-off? "
                "If travelling, when are you scheduled to get home?"
            ),
            "recovery_start_date": None,
        }
    return {"status": "none", "adjustment_paused": False, "action_required": None}
