"""Integrity tests for the committed demo dataset the whole demo depends on."""

from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    bounding_polygon,
    polygon_area_square_miles,
)
from certiroute.optimization import ScheduleStrategy, compare_schedules
from certiroute.sample_conditions import build_demo_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"

REQUIRED_COLUMNS = {
    "job_id",
    "name",
    "latitude",
    "longitude",
    "duration_minutes",
    "priority",
    "earliest_start",
    "latest_finish",
    "sample_temperature_c",
    "sample_certainty",
    "diurnal_amplitude",
}


@pytest.fixture(scope="module")
def sample_jobs() -> pd.DataFrame:
    return pd.read_csv(SAMPLE_PATH)


def test_sample_file_has_every_required_column(sample_jobs: pd.DataFrame) -> None:
    assert REQUIRED_COLUMNS <= set(sample_jobs.columns)
    assert not sample_jobs.isnull().to_numpy().any()


def test_job_ids_are_unique(sample_jobs: pd.DataFrame) -> None:
    assert sample_jobs["job_id"].is_unique


def test_every_row_builds_a_valid_domain_job(sample_jobs: pd.DataFrame) -> None:
    for row in sample_jobs.itertuples(index=False):
        job = Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
            earliest_start=time.fromisoformat(row.earliest_start),
            latest_finish=time.fromisoformat(row.latest_finish),
        )
        assert job.duration_minutes > 0
        assert 1 <= job.priority <= 5


def test_certainty_values_are_probabilities(sample_jobs: pd.DataFrame) -> None:
    assert sample_jobs["sample_certainty"].between(0, 1).all()


def test_each_job_fits_inside_its_own_time_window(sample_jobs: pd.DataFrame) -> None:
    for row in sample_jobs.itertuples(index=False):
        earliest = time.fromisoformat(row.earliest_start)
        latest = time.fromisoformat(row.latest_finish)
        window = (latest.hour * 60 + latest.minute) - (
            earliest.hour * 60 + earliest.minute
        )
        assert window >= row.duration_minutes, f"{row.job_id} cannot fit its window"


def test_sample_locations_stay_inside_the_demo_metro(sample_jobs: pd.DataFrame) -> None:
    assert sample_jobs["latitude"].between(33.0, 34.0).all()
    assert sample_jobs["longitude"].between(-113.0, -111.0).all()


def test_sample_aoi_fits_the_smallest_documented_plan_limit(
    sample_jobs: pd.DataFrame,
) -> None:
    points = [
        GeoPoint(latitude=row.latitude, longitude=row.longitude)
        for row in sample_jobs.itertuples(index=False)
    ]

    area = polygon_area_square_miles(bounding_polygon(points))

    assert area <= DEFAULT_MAX_AOI_AREA_SQUARE_MILES


def test_the_committed_demo_day_is_feasible_for_every_strategy(
    sample_jobs: pd.DataFrame,
) -> None:
    jobs = []
    profiles = {}
    for row in sample_jobs.itertuples(index=False):
        jobs.append(
            Job(
                job_id=row.job_id,
                name=row.name,
                location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
                duration_minutes=row.duration_minutes,
                priority=row.priority,
                earliest_start=time.fromisoformat(row.earliest_start),
                latest_finish=time.fromisoformat(row.latest_finish),
            )
        )
        profiles[row.job_id] = build_demo_profile(
            job_id=row.job_id,
            anchor_temperature_c=row.sample_temperature_c,
            certainty=row.sample_certainty,
            diurnal_amplitude=row.diurnal_amplitude,
        )

    depot = GeoPoint(latitude=33.44855, longitude=-112.07391)
    plans = compare_schedules(jobs, profiles, depot=depot)

    assert set(plans) == set(ScheduleStrategy)
    for plan in plans.values():
        assert len(plan.stops) == len(jobs)
        assert plan.route_finish_minute <= 17 * 60


def test_the_demo_scenario_still_shows_a_certainty_driven_difference(
    sample_jobs: pd.DataFrame,
) -> None:
    """The stress test is the demo's core claim, so guard it against drift."""

    jobs = []
    profiles = {}
    for row in sample_jobs.itertuples(index=False):
        jobs.append(
            Job(
                job_id=row.job_id,
                name=row.name,
                location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
                duration_minutes=row.duration_minutes,
                priority=row.priority,
                earliest_start=time.fromisoformat(row.earliest_start),
                latest_finish=time.fromisoformat(row.latest_finish),
            )
        )
        certainty = 0.15 if row.job_id == "PHX-101" else row.sample_certainty
        profiles[row.job_id] = build_demo_profile(
            job_id=row.job_id,
            anchor_temperature_c=row.sample_temperature_c,
            certainty=certainty,
            diurnal_amplitude=row.diurnal_amplitude,
        )

    depot = GeoPoint(latitude=33.44855, longitude=-112.07391)
    plans = compare_schedules(jobs, profiles, depot=depot, uncertainty_penalty=1.0)

    heat_aware = plans[ScheduleStrategy.HEAT_AWARE]
    certainty_aware = plans[ScheduleStrategy.CERTAINTY_AWARE]

    # The certainty-aware plan must not be worse on the uncertainty-adjusted
    # measure it optimizes; that is the entire product claim.
    assert (
        certainty_aware.total_adjusted_exposure_units
        <= heat_aware.total_adjusted_exposure_units
    )
