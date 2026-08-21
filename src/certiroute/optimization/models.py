"""Data models used by CertiRoute's scheduling strategies."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _adjusted_excess_at(
    minute: float,
    *,
    t0: float,
    t1: float,
    temp0: float,
    temp1: float,
    cert0: float,
    cert1: float,
    reference_c: float,
    uncertainty_penalty: float,
) -> float:
    """Evaluate certainty-adjusted positive temperature excess on one segment."""

    fraction = (minute - t0) / (t1 - t0)
    temperature = temp0 + fraction * (temp1 - temp0)
    certainty = cert0 + fraction * (cert1 - cert0)
    excess = max(temperature - reference_c, 0.0)
    return excess * (1 + uncertainty_penalty * (1 - certainty))


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

    def _linear_pieces(
        self, start_minute: float, end_minute: float
    ) -> list[tuple[float, float, float, float, float, float]]:
        """Split [start, end] into exactly linear (t0, t1, T0, T1, c0, c1) pieces."""

        if end_minute <= start_minute:
            raise ValueError("end_minute must be greater than start_minute")
        breakpoints = [start_minute]
        breakpoints.extend(
            float(point.minute_of_day)
            for point in self.points
            if start_minute < point.minute_of_day < end_minute
        )
        breakpoints.append(end_minute)
        pieces = []
        for left, right in zip(breakpoints, breakpoints[1:], strict=False):
            left_temp, left_cert = self.condition_at(left)
            right_temp, right_cert = self.condition_at(right)
            pieces.append((left, right, left_temp, right_temp, left_cert, right_cert))
        return pieces

    def mean_temperature(self, start_minute: float, end_minute: float) -> float:
        """Return the time-weighted mean temperature over the interval."""

        total = sum(
            (t1 - t0) * (temp0 + temp1) / 2
            for t0, t1, temp0, temp1, _, _ in self._linear_pieces(
                start_minute, end_minute
            )
        )
        return total / (end_minute - start_minute)

    def mean_certainty(self, start_minute: float, end_minute: float) -> float:
        """Return the time-weighted mean certainty over the interval."""

        total = sum(
            (t1 - t0) * (cert0 + cert1) / 2
            for t0, t1, _, _, cert0, cert1 in self._linear_pieces(
                start_minute, end_minute
            )
        )
        return total / (end_minute - start_minute)

    def degree_hours_above(
        self, reference_c: float, start_minute: float, end_minute: float
    ) -> float:
        """Integrate max(temperature - reference, 0) exactly over the interval."""

        degree_minutes = 0.0
        for t0, t1, temp0, temp1, _, _ in self._linear_pieces(start_minute, end_minute):
            excess0 = temp0 - reference_c
            excess1 = temp1 - reference_c
            width = t1 - t0
            if excess0 <= 0 and excess1 <= 0:
                continue
            if excess0 >= 0 and excess1 >= 0:
                degree_minutes += (excess0 + excess1) / 2 * width
                continue
            crossing = t0 + width * excess0 / (excess0 - excess1)
            if excess0 > 0:
                degree_minutes += excess0 * (crossing - t0) / 2
            else:
                degree_minutes += excess1 * (t1 - crossing) / 2
        return degree_minutes / 60

    def certainty_adjusted_degree_hours_above(
        self,
        reference_c: float,
        start_minute: float,
        end_minute: float,
        *,
        uncertainty_penalty: float,
    ) -> float:
        """Integrate exposure with a pointwise certainty penalty.

        Each temperature and certainty segment is linear, so their product is
        quadratic. Simpson's rule is therefore exact on every positive-excess
        portion of a segment.
        """

        if uncertainty_penalty < 0:
            raise ValueError("uncertainty_penalty cannot be negative")

        adjusted_degree_minutes = 0.0
        for t0, t1, temp0, temp1, cert0, cert1 in self._linear_pieces(
            start_minute, end_minute
        ):
            if temp0 <= reference_c and temp1 <= reference_c:
                continue

            left = t0
            right = t1
            if temp0 <= reference_c < temp1:
                left = t0 + (t1 - t0) * (reference_c - temp0) / (temp1 - temp0)
            elif temp0 > reference_c >= temp1:
                right = t0 + (t1 - t0) * (reference_c - temp0) / (temp1 - temp0)

            midpoint = (left + right) / 2
            common = {
                "t0": t0,
                "t1": t1,
                "temp0": temp0,
                "temp1": temp1,
                "cert0": cert0,
                "cert1": cert1,
                "reference_c": reference_c,
                "uncertainty_penalty": uncertainty_penalty,
            }
            adjusted_degree_minutes += (
                (right - left)
                / 6
                * (
                    _adjusted_excess_at(left, **common)
                    + 4 * _adjusted_excess_at(midpoint, **common)
                    + _adjusted_excess_at(right, **common)
                )
            )

        return adjusted_degree_minutes / 60

    def peak_temperature(self, start_minute: float, end_minute: float) -> float:
        """Return the maximum interpolated temperature over an interval."""

        pieces = self._linear_pieces(start_minute, end_minute)
        return max(
            temperature
            for _, _, left_temperature, right_temperature, _, _ in pieces
            for temperature in (left_temperature, right_temperature)
        )

    def minutes_at_or_above(
        self, threshold_c: float, start_minute: float, end_minute: float
    ) -> float:
        """Return the fractional minutes with temperature at or above threshold."""

        minutes = 0.0
        for t0, t1, temp0, temp1, _, _ in self._linear_pieces(start_minute, end_minute):
            above0 = temp0 >= threshold_c
            above1 = temp1 >= threshold_c
            width = t1 - t0
            if above0 and above1:
                minutes += width
                continue
            if not above0 and not above1:
                continue
            crossing = t0 + width * (threshold_c - temp0) / (temp1 - temp0)
            minutes += (crossing - t0) if above0 else (t1 - crossing)
        return minutes


class ScheduledStop(BaseModel):
    """One scheduled job plus the risk estimate over its assigned interval.

    Temperature and certainty are time-weighted means over the job interval,
    and exposure integrates the interpolated profile rather than sampling the
    interval midpoint.
    """

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
    peak_temperature_c: float
    certainty: float = Field(ge=0, le=1)
    raw_exposure_units: float = Field(ge=0)
    certainty_adjusted_units: float = Field(ge=0)
    minutes_above_planning_threshold: float = Field(ge=0)


class SchedulePlan(BaseModel):
    """A complete, comparable crew schedule."""

    model_config = ConfigDict(frozen=True)

    strategy: ScheduleStrategy
    stops: tuple[ScheduledStop, ...]
    total_travel_minutes: int = Field(ge=0)
    total_raw_exposure_units: float = Field(ge=0)
    total_adjusted_exposure_units: float = Field(ge=0)
    minutes_above_planning_threshold: float = Field(ge=0)
    priority_weighted_delay_minutes: float = Field(ge=0)
    route_finish_minute: int
    objective_value: float = Field(ge=0)
