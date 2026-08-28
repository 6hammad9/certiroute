"""Tests for the distilled measurements a deployment ships with."""

from datetime import date
from pathlib import Path

from certiroute.measured import DEFAULT_PROFILE_PATH, daily_peaks

ROOT = Path(__file__).resolve().parents[1]


def test_daily_peaks_report_each_day_hottest_measured_moment() -> None:
    peaks = daily_peaks("phoenix", path=ROOT / DEFAULT_PROFILE_PATH)

    assert len(peaks) >= 20
    assert all(isinstance(day, date) for day in peaks)
    assert all(30.0 < value < 55.0 for value in peaks.values())


def test_a_heat_limit_means_different_things_in_different_cities() -> None:
    """Why the interface shows the area's own range beside the limit field.

    Forty degrees sounds cautious, but it is a ceiling almost every Phoenix day
    reaches and one no Miami day comes near. A dispatcher choosing a number
    without seeing their own area is choosing blind.
    """

    phoenix = daily_peaks("phoenix", path=ROOT / DEFAULT_PROFILE_PATH)
    miami = daily_peaks("miami", path=ROOT / DEFAULT_PROFILE_PATH)

    breached_in_phoenix = sum(1 for peak in phoenix.values() if peak >= 40.0)
    breached_in_miami = sum(1 for peak in miami.values() if peak >= 40.0)

    assert breached_in_phoenix > len(phoenix) * 0.5
    assert breached_in_miami == 0


def test_an_unknown_area_has_no_peaks_rather_than_failing() -> None:
    assert daily_peaks("nowhere", path=ROOT / DEFAULT_PROFILE_PATH) == {}


def test_a_missing_file_is_empty_rather_than_an_error(tmp_path) -> None:
    """Guidance is a convenience, so its absence must not block planning."""

    assert daily_peaks("phoenix", path=tmp_path / "absent.json") == {}
