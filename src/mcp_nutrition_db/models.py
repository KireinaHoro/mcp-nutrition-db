"""Validated public models and calendar-window resolution."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_TIMEZONE = "Europe/Zurich"
MAX_QUERY_DAYS = 366


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class EntryKind(StrEnum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"
    OTHER = "other"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceType(StrEnum):
    ESTIMATED = "estimated"
    NUTRITION_LABEL = "nutrition_label"
    RESTAURANT_DECLARED = "restaurant_declared"
    DATABASE = "database"
    USER_PROVIDED = "user_provided"
    MIXED = "mixed"
    OTHER = "other"


class TrainingSourceType(StrEnum):
    ESTIMATED = "estimated"
    WEARABLE = "wearable"
    FITNESS_MACHINE = "fitness_machine"
    APP = "app"
    USER_PROVIDED = "user_provided"
    OTHER = "other"


class NutritionValues(StrictModel):
    calories_kcal: float | None = Field(default=None, ge=0, le=100_000)
    protein_g: float | None = Field(default=None, ge=0, le=10_000)
    carbohydrate_g: float | None = Field(default=None, ge=0, le=10_000)
    fat_g: float | None = Field(default=None, ge=0, le=10_000)
    fiber_g: float | None = Field(default=None, ge=0, le=10_000)
    sugar_g: float | None = Field(default=None, ge=0, le=10_000)
    sodium_mg: float | None = Field(default=None, ge=0, le=10_000_000)

    @model_validator(mode="after")
    def require_value(self) -> NutritionValues:
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one nutrition value is required")
        return self


class NutritionSource(StrictModel):
    type: SourceType
    detail: str | None = Field(default=None, max_length=500)


class ComponentInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: str | None = Field(default=None, min_length=1, max_length=40)
    portion_notes: str | None = Field(default=None, max_length=1_000)
    source: NutritionSource
    nutrition: NutritionValues

    @model_validator(mode="after")
    def quantity_and_unit_together(self) -> ComponentInput:
        if (self.quantity is None) != (self.unit is None):
            raise ValueError("quantity and unit must either both be set or both be omitted")
        return self


class Estimation(StrictModel):
    confidence: Confidence
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    source: str = Field(min_length=1, max_length=200)

    @field_validator("assumptions")
    @classmethod
    def validate_assumptions(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 500 for value in values):
            raise ValueError("assumptions must contain non-empty strings of at most 500 characters")
        return values


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone: {value}") from error
    return value


def validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value


class LogEntryInput(StrictModel):
    occurred_at: datetime
    kind: EntryKind
    title: str = Field(min_length=1, max_length=300)
    components: list[ComponentInput] = Field(min_length=1, max_length=100)
    timezone: str = DEFAULT_TIMEZONE
    notes: str | None = Field(default=None, max_length=5_000)
    estimation: Estimation | None = None
    force_new: bool = False

    _aware_occurred_at = field_validator("occurred_at")(validate_aware_datetime)
    _valid_timezone = field_validator("timezone")(validate_timezone)


class EntryChanges(StrictModel):
    occurred_at: datetime | None = None
    timezone: str | None = None
    kind: EntryKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    notes: str | None = Field(default=None, max_length=5_000)
    components: list[ComponentInput] | None = Field(default=None, min_length=1, max_length=100)
    estimation: Estimation | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware_if_set(cls, value: datetime | None) -> datetime | None:
        return None if value is None else validate_aware_datetime(value)

    @field_validator("timezone")
    @classmethod
    def timezone_if_set(cls, value: str | None) -> str | None:
        return None if value is None else validate_timezone(value)

    @model_validator(mode="after")
    def require_change(self) -> EntryChanges:
        if not self.model_fields_set:
            raise ValueError("at least one changed field is required")
        return self


class RelativeDayWindow(StrictModel):
    type: Literal["relative_day"]
    day: Literal["today", "yesterday"]
    timezone: str = DEFAULT_TIMEZONE

    _valid_timezone = field_validator("timezone")(validate_timezone)


class CalendarDayWindow(StrictModel):
    type: Literal["calendar_day"]
    date: date
    timezone: str = DEFAULT_TIMEZONE

    _valid_timezone = field_validator("timezone")(validate_timezone)


class IntervalWindow(StrictModel):
    type: Literal["interval"]
    start: datetime
    end: datetime
    timezone: str = DEFAULT_TIMEZONE

    _aware_start = field_validator("start")(validate_aware_datetime)
    _aware_end = field_validator("end")(validate_aware_datetime)
    _valid_timezone = field_validator("timezone")(validate_timezone)

    @model_validator(mode="after")
    def validate_range(self) -> IntervalWindow:
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        if self.end - self.start > timedelta(days=MAX_QUERY_DAYS):
            raise ValueError(f"window cannot exceed {MAX_QUERY_DAYS} days")
        return self


type QueryWindow = Annotated[
    RelativeDayWindow | CalendarDayWindow | IntervalWindow,
    Field(discriminator="type"),
]


class ResolvedWindow(StrictModel):
    start: datetime
    end: datetime
    timezone: str


def resolve_window(window: QueryWindow, *, now: datetime | None = None) -> ResolvedWindow:
    zone = ZoneInfo(window.timezone)
    current = now or datetime.now(UTC)
    current = validate_aware_datetime(current)

    if isinstance(window, IntervalWindow):
        start = window.start.astimezone(UTC)
        end = window.end.astimezone(UTC)
    else:
        if isinstance(window, RelativeDayWindow):
            local_date = current.astimezone(zone).date()
            if window.day == "yesterday":
                local_date -= timedelta(days=1)
        else:
            local_date = window.date
        start_local = datetime.combine(local_date, time.min, tzinfo=zone)
        end_local = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
        start = start_local.astimezone(UTC)
        end = end_local.astimezone(UTC)

    return ResolvedWindow(start=start, end=end, timezone=window.timezone)


class ListEntriesInput(StrictModel):
    window: QueryWindow
    kind: EntryKind | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class TrainingSource(StrictModel):
    type: TrainingSourceType
    detail: str | None = Field(default=None, max_length=500)


class LogTrainingInput(StrictModel):
    occurred_at: datetime
    activity: str = Field(min_length=1, max_length=200)
    duration_minutes: float = Field(gt=0, le=10_080)
    calories_burned_kcal: float = Field(gt=0, le=100_000)
    source: TrainingSource
    timezone: str = DEFAULT_TIMEZONE
    notes: str | None = Field(default=None, max_length=5_000)
    force_new: bool = False

    _aware_occurred_at = field_validator("occurred_at")(validate_aware_datetime)
    _valid_timezone = field_validator("timezone")(validate_timezone)


class TrainingChanges(StrictModel):
    occurred_at: datetime | None = None
    timezone: str | None = None
    activity: str | None = Field(default=None, min_length=1, max_length=200)
    duration_minutes: float | None = Field(default=None, gt=0, le=10_080)
    calories_burned_kcal: float | None = Field(default=None, gt=0, le=100_000)
    source: TrainingSource | None = None
    notes: str | None = Field(default=None, max_length=5_000)

    @field_validator("occurred_at")
    @classmethod
    def aware_if_set(cls, value: datetime | None) -> datetime | None:
        return None if value is None else validate_aware_datetime(value)

    @field_validator("timezone")
    @classmethod
    def timezone_if_set(cls, value: str | None) -> str | None:
        return None if value is None else validate_timezone(value)

    @model_validator(mode="after")
    def require_change(self) -> TrainingChanges:
        if not self.model_fields_set:
            raise ValueError("at least one changed field is required")
        return self


class ListTrainingsInput(StrictModel):
    window: QueryWindow
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class SummarizeInput(StrictModel):
    window: QueryWindow
    grouping: Literal["day", "whole_range"] = "whole_range"


class GoalInput(StrictModel):
    effective_from: date
    timezone: str = DEFAULT_TIMEZONE
    base_burn_kcal: float = Field(gt=0, le=100_000)
    deficit_kcal: float = Field(default=0, ge=0, le=100_000)
    targets: NutritionValues | None = None
    reason: str = Field(min_length=1, max_length=500)

    _valid_timezone = field_validator("timezone")(validate_timezone)

    @model_validator(mode="after")
    def calories_are_derived(self) -> GoalInput:
        if self.targets is not None and self.targets.calories_kcal is not None:
            raise ValueError(
                "targets.calories_kcal is derived; set base_burn_kcal and deficit_kcal instead"
            )
        if self.deficit_kcal >= self.base_burn_kcal:
            raise ValueError("deficit_kcal must be less than base_burn_kcal")
        return self
