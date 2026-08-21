import pytest

from certiroute.optimization import ConditionPoint, TemperatureProfile


def make_ramp_profile() -> TemperatureProfile:
    """Linear ramp: 26 °C at 08:00 rising to 34 °C at 10:00."""

    return TemperatureProfile(
        job_id="A",
        points=(
            ConditionPoint(minute_of_day=480, temperature_c=26, certainty=0.9),
            ConditionPoint(minute_of_day=600, temperature_c=34, certainty=0.5),
        ),
    )


def test_degree_hours_handles_reference_crossing() -> None:
    profile = make_ramp_profile()

    # The ramp crosses 30 °C at minute 540, leaving a 0→4 °C triangle over
    # the final 60 minutes: 120 degree-minutes, i.e. 2 degree-hours.
    assert profile.degree_hours_above(30, 480, 600) == pytest.approx(2.0)


def test_minutes_at_or_above_is_fractional() -> None:
    profile = make_ramp_profile()

    assert profile.minutes_at_or_above(30, 480, 600) == pytest.approx(60.0)
    assert profile.minutes_at_or_above(30, 480, 540) == pytest.approx(0.0)
    assert profile.minutes_at_or_above(30, 510, 570) == pytest.approx(30.0)


def test_means_are_time_weighted() -> None:
    profile = make_ramp_profile()

    assert profile.mean_temperature(480, 600) == pytest.approx(30.0)
    assert profile.mean_certainty(480, 600) == pytest.approx(0.7)


def test_intervals_beyond_profile_edges_hold_endpoint_values() -> None:
    profile = make_ramp_profile()

    # After the last point the profile holds 34 °C: 4 °C excess for 30 min.
    assert profile.degree_hours_above(30, 600, 630) == pytest.approx(2.0)


def test_interval_methods_reject_empty_intervals() -> None:
    profile = make_ramp_profile()

    with pytest.raises(ValueError, match="greater than start"):
        profile.mean_temperature(600, 600)


def test_pointwise_certainty_adjustment_matches_closed_form() -> None:
    profile = make_ramp_profile()

    # Positive excess only on [540, 600]. With u = (t - 540) / 60 the excess
    # is 4u °C, certainty is 0.7 - 0.2u, and a penalty of 1 gives the
    # multiplier 1.3 + 0.2u. Integrating 4u(1.3 + 0.2u) exactly yields
    # 43/15 degree-hours, which Simpson's rule must reproduce.
    adjusted = profile.certainty_adjusted_degree_hours_above(
        30, 480, 600, uncertainty_penalty=1.0
    )

    assert adjusted == pytest.approx(43 / 15)
    # A zero penalty must collapse to the unadjusted integral.
    assert profile.certainty_adjusted_degree_hours_above(
        30, 480, 600, uncertainty_penalty=0.0
    ) == pytest.approx(profile.degree_hours_above(30, 480, 600))


def test_peak_temperature_over_intervals() -> None:
    profile = make_ramp_profile()

    assert profile.peak_temperature(480, 600) == pytest.approx(34.0)
    assert profile.peak_temperature(500, 560) == pytest.approx(26 + 8 * 80 / 120)
