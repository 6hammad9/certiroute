"""Data models used by CertiRoute's scheduling strategies."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScheduleStrategy(StrEnum):
    """The four schedules shown side by side in the product demo."""

    ORIGINAL = "Original order"
    EFFICIENCY = "Efficiency only"
    HEAT_AWARE = "Heat aware"
    CERTAINTY_AWARE = "Certainty aware"


class ConditionPoint(BaseModel):
    """Temperature and certainty at one minute of the demo day."""

    model_config = ConfigDict(frozen=True)

    minute_of_day: int = Field(ge=0, lt=24 * 60)
    temperature_c: float
    certainty: float = Field(ge=0, le=1)


class TemperatureProfile(BaseModel):
    """A linearly interpolated temperature profile for one job location."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1)
    points: tuple[ConditionPoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> "TemperatureProfile":
        minutes = [point.minute_of_day for point in self.points]
        if minutes != sorted(minutes):
            raise ValueError("profile points must be ordered by minute_of_day")
        if len(minutes) != len(set(minutes)):
            raise ValueError("profile points cannot contain duplicate minutes")
        return self

    def condition_at(self, minute_of_day: float) -> tuple[float, float]:
        """Return linearly interpolated temperature and certainty."""

        if minute_of_day <= self.points[0].minute_of_day:
            first = self.points[0]
            return first.temperature_c, first.certainty
        if minute_of_day >= self.points[-1].minute_of_day:
            last = self.points[-1]
            return last.temperature_c, last.certainty

        for left, right in zip(self.points, self.points[1:], strict=False):
            if left.minute_of_day <= minute_of_day <= right.minute_of_day:
                width = right.minute_of_day - left.minute_of_day
                fraction = (minute_of_day - left.minute_of_day) / width
                temperature = left.temperature_c + fraction * (
                    right.temperature_c - left.temperature_c
                )
                certainty = left.certainty + fraction * (
                    right.certainty - left.certainty
                )
                return temperature, certainty

        raise RuntimeError("profile interpolation failed")


class ScheduledStop(BaseModel):
    """One scheduled job plus the risk estimate at its assigned time."""

    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    job_id: str
    job_name: str
    latitude: float
    longitude: float
    arrival_minute: int
    start_minute: int
    finish_minute: int
    inbound_travel_minutes: int = Field(ge=0)
    temperature_c: float
    certainty: float = Field(ge=0, le=1)
    raw_exposure_units: float = Field(ge=0)
    certainty_adjusted_units: float = Field(ge=0)


class SchedulePlan(BaseModel):
    """A complete, comparable crew schedule."""

    model_config = ConfigDict(frozen=True)

    strategy: ScheduleStrategy
    stops: tuple[ScheduledStop, ...]
    total_travel_minutes: int = Field(ge=0)
    total_raw_exposure_units: float = Field(ge=0)
    total_adjusted_exposure_units: float = Field(ge=0)
    minutes_above_planning_threshold: int = Field(ge=0)
    route_finish_minute: int
    objective_value: float = Field(ge=0)
