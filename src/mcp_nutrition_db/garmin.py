"""Replaceable Garmin read adapter and conservative normalized fact validation."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .serialization import canonical_json, timestamp


class GarminProvider(Protocol):
    account_id: str

    def weights(self, start: date, end: date) -> list[dict[str, Any]]: ...
    def activities(self, start: date, end: date) -> list[dict[str, Any]]: ...


WEIGHT_FIELDS = ("samplePk", "date", "weight", "sourceType")
ACTIVITY_FIELDS = (
    "activityId",
    "activityName",
    "startTimeGMT",
    "startTimeLocal",
    "movingDuration",
    "duration",
    "calories",
    "bmrCalories",
    "activeCalories",
    "activityType",
    "parentId",
    "parentActivityId",
    "childIds",
    "isParent",
    "deviceId",
    "sensors",
    "averageHR",
    "distance",
)


class GarminAdapter:
    def __init__(self, api: Any, account_id: str) -> None:
        self.api = api
        self.account_id = account_id

    def weights(self, start: date, end: date) -> list[dict[str, Any]]:
        response = self.api.get_weigh_ins(start.isoformat(), end.isoformat())
        if not isinstance(response, dict) or not isinstance(
            response.get("dailyWeightSummaries"), list
        ):
            raise ValueError("unrecognized weight response; coverage not advanced")
        records: list[dict[str, Any]] = []
        for day in response["dailyWeightSummaries"]:
            values = day.get("allWeightMetrics")
            if not isinstance(values, list):
                raise ValueError("incomplete weight response")
            records.extend({k: r[k] for k in WEIGHT_FIELDS if k in r} for r in values)
        return records

    def activities(self, start: date, end: date) -> list[dict[str, Any]]:
        # Explicit pages: any malformed, repeated or incomplete fetch aborts the chunk.
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for offset in range(0, 10_000, 100):
            page = self.api.connectapi(
                self.api.garmin_connect_activities,
                params={
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "start": offset,
                    "limit": 100,
                },
            )
            if not isinstance(page, list):
                raise ValueError("invalid activity page")
            if not page:
                return records
            for summary in page:
                identity = permanent_id(summary.get("activityId"))
                if identity in seen:
                    raise ValueError("repeated activity pagination; retry complete chunk")
                seen.add(identity)
                detail = self.api.get_activity(identity)
                if permanent_id(detail.get("activityId")) != identity:
                    raise ValueError("activity detail ID mismatch")
                merged = {
                    **summary,
                    **detail.get("summaryDTO", {}),
                    **{k: v for k, v in detail.items() if k != "summaryDTO"},
                }
                metadata = detail.get("metadataDTO") or {}
                sensors = metadata.get("sensors")
                if isinstance(sensors, list):
                    merged["sensors"] = [
                        {
                            k: sensor[k]
                            for k in ("manufacturer", "sourceType", "antplusDeviceType")
                            if k in sensor
                        }
                        for sensor in sensors
                        if isinstance(sensor, dict)
                    ]
                merged["isParent"] = bool(
                    merged.get("isParent") or detail.get("isMultiSportParent")
                )
                if metadata.get("childIds"):
                    merged["childIds"] = metadata["childIds"]
                records.append({k: merged[k] for k in ACTIVITY_FIELDS if k in merged})
        raise ValueError("activity pagination limit exceeded")


def permanent_id(value: Any) -> str:
    if isinstance(value, bool) or not str(value).isdigit() or int(str(value)) <= 0:
        raise ValueError("missing permanent external ID")
    return str(value)


def scaled(value: Any, factor: int, *, positive: bool = False) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError("missing numeric component")
    try:
        number = Decimal(str(value)) * factor
        if not number.is_finite() or number < 0 or (positive and number <= 0):
            raise ValueError("invalid numeric component")
        return int(number.to_integral_value())
    except InvalidOperation:
        raise ValueError("invalid numeric component") from None


def source_hash(facts: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(facts).encode()).hexdigest()


def normalize(stream: str, raw: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    fields = WEIGHT_FIELDS if stream == "weight" else ACTIVITY_FIELDS
    raw = {k: raw[k] for k in fields if k in raw}
    issues: list[str] = []
    facts: dict[str, Any] = {"raw": raw, "issues": issues}
    try:
        facts["external_id"] = permanent_id(
            raw.get("samplePk" if stream == "weight" else "activityId")
        )
    except ValueError:
        facts["external_id"] = "unverified-" + source_hash(raw)
        issues.append("unverified_identity")
    try:
        if stream == "weight":
            if not validation.get("weight_grams_epoch_ms"):
                issues.append("weight_units_and_timestamp_unverified")
            measured = datetime.fromtimestamp(scaled(raw.get("date"), 1) / 1000, UTC)
            facts.update(
                measured_at=timestamp(measured),
                weight_grams=scaled(raw.get("weight"), 1, positive=True),
                source="Garmin Connect / " + str(raw.get("sourceType", "scale")),
            )
            if facts["weight_grams"] > 1_000_000:
                issues.append("weight_out_of_range")
        else:
            if not validation.get("activity_ids_utc_seconds"):
                issues.append("activity_identity_timestamp_duration_unverified")
            occurred = datetime.fromisoformat(str(raw.get("startTimeGMT")))
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=UTC)
            facts["occurred_at"] = timestamp(occurred)
            moving = (
                scaled(raw["movingDuration"], 1000) if raw.get("movingDuration") is not None else 0
            )
            timer = scaled(raw["duration"], 1000) if raw.get("duration") is not None else 0
            facts.update(moving_ms=moving, timer_ms=timer, duration_ms=moving or timer)
            if not facts["duration_ms"]:
                issues.append("missing_positive_duration")
            explicit = (
                scaled(raw["activeCalories"], 1000)
                if raw.get("activeCalories") is not None
                else None
            )
            total = scaled(raw["calories"], 1000) if raw.get("calories") is not None else None
            resting = (
                scaled(raw["bmrCalories"], 1000) if raw.get("bmrCalories") is not None else None
            )
            derived = total - resting if total is not None and resting is not None else None
            if derived is not None and derived < 0:
                issues.append("contradictory_calories")
            if explicit is not None and derived is not None and explicit != derived:
                issues.append("contradictory_calories")
            if explicit is not None and total is not None and explicit > total:
                issues.append("contradictory_calories")
            active = None
            rule = None
            if validation.get("explicit_active_calories") and explicit is not None:
                active, rule = explicit, "explicit_active"
            elif validation.get("total_minus_resting") and derived is not None:
                active, rule = derived, "total_minus_resting"
            if active is None or active <= 0:
                issues.append("active_calories_unverified_or_nonpositive")
            facts.update(active_mkcal=active, calorie_rule=rule)
            activity_type = raw.get("activityType") or {}
            activity_type = (
                activity_type.get("typeKey", "unknown")
                if isinstance(activity_type, dict)
                else str(activity_type)
            )
            if (
                any(raw.get(k) for k in ("parentId", "parentActivityId", "childIds", "isParent"))
                or activity_type == "multi_sport"
            ):
                issues.append("multisport_selection_required")
            facts["activity"] = str(raw.get("activityName") or activity_type)[:200]
            sensors = raw.get("sensors")
            power_sensors = (
                [
                    sensor
                    for sensor in sensors
                    if isinstance(sensor, dict)
                    and sensor.get("sourceType") == "ANTPLUS"
                    and sensor.get("antplusDeviceType") == "BIKE_POWER"
                ]
                if isinstance(sensors, list)
                else []
            )
            cycling = activity_type in {
                "cycling",
                "road_biking",
                "indoor_cycling",
                "mountain_biking",
                "gravel_cycling",
                "cyclocross",
                "track_cycling",
                "recumbent_cycling",
            }
            power = bool(validation.get("bike_power_sensor_metadata") and power_sensors and cycling)
            if power:
                facts["evidence"] = {
                    "device": ", ".join(
                        str(sensor.get("manufacturer") or "Unknown manufacturer")
                        for sensor in power_sensors
                    ),
                    "detail": "Garmin activity metadata records an ANTPLUS BIKE_POWER sensor.",
                }
            facts["confidence"] = "high" if power else "medium"
            facts["measurement_method"] = (
                "power_meter"
                if power
                else (
                    "heart_rate_gps_model"
                    if raw.get("averageHR")
                    and raw.get("distance")
                    and activity_type in validation.get("hr_gps_activity_types", [])
                    else "device_estimate"
                )
            )
    except (ValueError, TypeError, OverflowError, OSError):
        issues.append("invalid_or_missing_fields")
    return facts
