"""Test a planned shift against an absolute heat limit.

Ranking start times is close to trivial in high summer: heat climbs from dawn
to mid-afternoon, so the earliest feasible start wins every time, and no model
is needed to say so. Measured across 24 Phoenix days, the earliest start was
coolest on all of them.

An absolute limit does not behave that way. Asking whether a shift stays under
40 C splits the same 24 days three ways - 8 where the usual start is already
fine, 12 where the shift has to move, and 4 where no start in the window works
at all and the day needs splitting or standing down. The verdict turns on the
day's level, which is the quantity FortyGuard measures and the one nobody can
judge by eye. That is where a reading earns its place: not in choosing the
cooler hour, but in saying whether the cooler hour is cool enough.

The check runs against the conservative profiles - the upper end of the
calibrated interval - rather than the expected ones. A threshold is a safety
question, so the honest test is the worst case the interval admits, not the
midpoint. A shift reported clear here is clear across the whole interval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from certiroute.optimization import ScheduledStop, TemperatureProfile

# Working intervals are sampled rather than solved because the profiles are
# piecewise linear and a stop can straddle several segments. Five minutes is
# fine enough that a crossing is never missed by more than that, and coarse
# enough to stay cheap across every candidate start.
SAMPLE_STEP_MINUTES = 5


class DayVerdict(Enum):
    """What the day asks of a dispatcher, once the limit is applied."""

    NO_ACTION = "no_action"
    MOVE_EARLIER = "move_earlier"
    NO_SAFE_START = "no_safe_start"


@dataclass(frozen=True)
class SiteBreach:
    """One site where the crew is over the limit while working."""

    job_id: str
    job_name: str
    first_minute: int
    last_minute: int
    minutes_over: int
    peak_c: float


@dataclass(frozen=True)
class ShiftLimitCheck:
    """Whether one candidate start keeps the whole shift under the limit."""

    limit_c: float
    shift_start_minute: int
    breaches: tuple[SiteBreach, ...]
    peak_c: float
    peak_job_name: str
    peak_minute: int
    # What the model actually expects, alongside the worst case it is judged
    # on. Planning a day ahead widens the interval to several degrees, and a
    # panel showing only the top of it reads as a forecast of disaster rather
    # than as the conservative test it is. The verdict still uses the worst
    # case; the reader gets to see how much of it is margin.
    expected_peak_c: float | None = None

    @property
    def clear(self) -> bool:
        return not self.breaches

    @property
    def margin_c(self) -> float | None:
        """How much of the peak is uncertainty rather than expected heat."""

        if self.expected_peak_c is None:
            return None
        return round(self.peak_c - self.expected_peak_c, 1)

    @property
    def minutes_over(self) -> int:
        return sum(breach.minutes_over for breach in self.breaches)

    @property
    def sites_over(self) -> int:
        return len(self.breaches)


@dataclass(frozen=True)
class DayLimitAssessment:
    """Every candidate start tested against the limit, and what that means."""

    limit_c: float
    checks: tuple[ShiftLimitCheck, ...]
    baseline_minute: int

    @property
    def baseline(self) -> ShiftLimitCheck:
        for check in self.checks:
            if check.shift_start_minute == self.baseline_minute:
                return check
        raise LookupError("the baseline start was not among the checks")

    @property
    def clear_starts(self) -> tuple[ShiftLimitCheck, ...]:
        return tuple(check for check in self.checks if check.clear)

    @property
    def earliest_clear(self) -> ShiftLimitCheck | None:
        """The latest start that still holds, so the crew moves as little as
        possible. Moving a shift has its own cost, and there is no safety
        gain in going earlier than the limit requires."""

        clear = self.clear_starts
        return max(clear, key=lambda check: check.shift_start_minute) if clear else None

    @property
    def verdict(self) -> DayVerdict:
        if self.baseline.clear:
            return DayVerdict.NO_ACTION
        if self.clear_starts:
            return DayVerdict.MOVE_EARLIER
        return DayVerdict.NO_SAFE_START


def check_stops_against_limit(
    stops: Sequence[ScheduledStop],
    profiles: Mapping[str, TemperatureProfile],
    *,
    limit_c: float,
    shift_start_minute: int,
    step_minutes: int = SAMPLE_STEP_MINUTES,
    expected_profiles: Mapping[str, TemperatureProfile] | None = None,
) -> ShiftLimitCheck:
    """Walk each stop's working interval and record where it sits over the limit.

    Only the minutes the crew is actually working are counted. Travel is left
    out deliberately: a crew in a moving vehicle is not the exposure this is
    about, and counting it would inflate every number without changing any
    ranking.

    ``expected_profiles`` is reported but never judged against. The verdict is
    always the worst case the interval admits; carrying the expected peak too
    only lets a reader see how much of a frightening number is margin.
    """

    if step_minutes <= 0:
        raise ValueError("step_minutes must be greater than zero")

    breaches: list[SiteBreach] = []
    peak_c = float("-inf")
    expected_peak_c = float("-inf")
    peak_job_name = ""
    peak_minute = shift_start_minute

    for stop in stops:
        profile = profiles.get(stop.job_id)
        if profile is None:
            raise LookupError(f"no temperature profile for {stop.job_id!r}")

        expected = (
            None if expected_profiles is None else expected_profiles.get(stop.job_id)
        )
        over_minutes: list[int] = []
        site_peak = float("-inf")
        minute = stop.start_minute
        while minute <= stop.finish_minute:
            temperature, _ = profile.condition_at(minute)
            if temperature > peak_c:
                peak_c, peak_job_name, peak_minute = temperature, stop.job_name, minute
            site_peak = max(site_peak, temperature)
            if temperature >= limit_c:
                over_minutes.append(minute)
            if expected is not None:
                expected_peak_c = max(expected_peak_c, expected.condition_at(minute)[0])
            minute += step_minutes

        if over_minutes:
            breaches.append(
                SiteBreach(
                    job_id=stop.job_id,
                    job_name=stop.job_name,
                    first_minute=over_minutes[0],
                    last_minute=over_minutes[-1],
                    # A crossing is only resolved to the sampling step, so the
                    # span is reported rather than the sample count, which
                    # would understate a breach caught late in an interval.
                    minutes_over=over_minutes[-1] - over_minutes[0] + step_minutes,
                    peak_c=round(site_peak, 1),
                )
            )

    if peak_c == float("-inf"):
        raise ValueError("a shift must contain at least one stop to check")

    return ShiftLimitCheck(
        limit_c=limit_c,
        shift_start_minute=shift_start_minute,
        breaches=tuple(breaches),
        peak_c=round(peak_c, 1),
        peak_job_name=peak_job_name,
        peak_minute=peak_minute,
        expected_peak_c=(
            None if expected_peak_c == float("-inf") else round(expected_peak_c, 1)
        ),
    )


def assess_day_against_limit(
    plans_by_start_minute: Mapping[int, Sequence[ScheduledStop]],
    profiles: Mapping[str, TemperatureProfile],
    *,
    limit_c: float,
    baseline_minute: int,
    step_minutes: int = SAMPLE_STEP_MINUTES,
    expected_profiles: Mapping[str, TemperatureProfile] | None = None,
) -> DayLimitAssessment:
    """Test every candidate start, and say what the day asks of a dispatcher."""

    if not plans_by_start_minute:
        raise ValueError("at least one candidate start is required")
    if baseline_minute not in plans_by_start_minute:
        raise ValueError("the baseline start must be among the candidates")

    checks = tuple(
        check_stops_against_limit(
            stops,
            profiles,
            limit_c=limit_c,
            shift_start_minute=start_minute,
            step_minutes=step_minutes,
            expected_profiles=expected_profiles,
        )
        for start_minute, stops in sorted(plans_by_start_minute.items())
    )
    return DayLimitAssessment(
        limit_c=limit_c, checks=checks, baseline_minute=baseline_minute
    )


__all__ = [
    "SAMPLE_STEP_MINUTES",
    "DayLimitAssessment",
    "DayVerdict",
    "ShiftLimitCheck",
    "SiteBreach",
    "assess_day_against_limit",
    "check_stops_against_limit",
]
