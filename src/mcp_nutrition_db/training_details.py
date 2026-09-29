"""Expose latest Garmin measurements separately from locally maintained training evidence."""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

# Garmin summaryDTO uses meters, seconds, m/s, bpm, watts and rpm.
METRICS = {
    "distance": "distance_m",
    "averageHR": "average_hr_bpm",
    "maxHR": "max_hr_bpm",
    "averagePower": "average_power_w",
    "maxPower": "max_power_w",
    "normalizedPower": "normalized_power_w",
    "averageBikeCadence": "average_cadence_rpm",
    "elevationGain": "elevation_gain_m",
    "elevationLoss": "elevation_loss_m",
    "averageSpeed": "average_speed_mps",
    "maxSpeed": "max_speed_mps",
    "movingDuration": "moving_duration_seconds",
    "duration": "timer_duration_seconds",
}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def add_garmin_details(connection: sqlite3.Connection, trainings: list[dict[str, Any]]) -> None:
    """Read source metadata in batches without changing records, revisions or accounting."""
    for offset in range(0, len(trainings), 100):
        indexed = {t["training_id"]: t for t in trainings[offset : offset + 100]}
        placeholders = ",".join("?" for _ in indexed)
        rows = connection.execute(
            "SELECT e.training_id,s.external_id,s.facts_json,s.status,s.fetched_at "
            "FROM external_records e JOIN import_staging s "
            "ON s.account_id=e.account_id AND s.external_id=e.external_id "
            "AND s.stream='activity' "
            f"WHERE e.suppressed=0 AND e.training_id IN ({placeholders})",
            list(indexed),
        )
        for row in rows:
            facts = json.loads(row["facts_json"])
            raw = facts.get("raw", {})
            metrics = {
                public: value
                for source, public in METRICS.items()
                if (value := number(raw.get(source))) is not None
                and (source not in ("averageHR", "maxHR") or value > 0)
            }
            active = number(facts.get("active_mkcal"))
            indexed[row["training_id"]]["garmin_activity"] = {
                "activity_id": row["external_id"],
                "fetched_at": row["fetched_at"],
                "sync_status": row["status"],
                "metrics": metrics,
                "energy": {
                    "basis": "active",
                    "active_kcal": None if active is None else active / 1000,
                    "total_kcal": number(raw.get("calories")),
                    "resting_kcal": number(raw.get("bmrCalories")),
                    "mapping": facts.get("calorie_rule"),
                    "note": "Training reported_burn_kcal uses per-activity active calories, "
                    "excluding resting calories. Confidence is applied once to produce "
                    "credited_burn_kcal. These are the latest Garmin source measurements; "
                    "pending changes or local overrides can differ from the accounted training.",
                },
            }
