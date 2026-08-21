"""Integrity tests for the operational demonstration work orders."""

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
from certiroute.optimization import (
    ConditionPoint,
    ScheduleStrategy,
    TemperatureProfile,
    compare_schedules,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)

REQUIRED_COLUMNS = {
    "job_id",
    "name",
    "latitude",
    "longitude",
    "duration_minutes",
    "priority",
    "earliest_start",
    "latest_finish",
}


@pytest.fixture(scope="module")
def sample_jobs() -> pd.DataFrame:
    return pd.read_csv(SAMPLE_PATH)


def _domain_jobs(frame: pd.DataFrame) -> list[Job]:
    return [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
            earliest_start=time.fromisoformat(row.earliest_start),
            latest_finish=time.fromisoformat(row.latest_finish),
        )
        for row in frame.itertuples(index=False)
    ]


def test_sample_file_contains_only_the_operational_contract(
    sample_jobs: pd.DataFrame,
) -> None:
    assert set(sample_jobs.columns) == REQUIRED_COLUMNS
    assert len(sample_jobs) == 6
    assert not sample_jobs.isnull().to_numpy().any()


def test_job_ids_are_unique(sample_jobs: pd.DataFrame) -> None:
    assert sample_jobs["job_id"].is_unique
    assert set(sample_jobs["job_id"]) == {
        "PHX-201",
        "PHX-202",
        "PHX-203",
        "PHX-204",
        "PHX-205",
        "PHX-206",
    }


def test_every_row_builds_a_valid_domain_job(sample_jobs: pd.DataFrame) -> None:
    for job in _domain_jobs(sample_jobs):
        assert job.duration_minutes > 0
        assert 1 <= job.priority <= 5


def test_each_job_fits_inside_its_own_time_window(
    sample_jobs: pd.DataFrame,
) -> None:
    for row in sample_jobs.itertuples(index=False):
        earliest = time.fromisoformat(row.earliest_start)
        latest = time.fromisoformat(row.latest_finish)
        window = (latest.hour * 60 + latest.minute) - (
            earliest.hour * 60 + earliest.minute
        )
        assert window >= row.duration_minutes, f"{row.job_id} cannot fit its window"


def test_sample_locations_stay_inside_phoenix(sample_jobs: pd.DataFrame) -> None:
    assert sample_jobs["latitude"].between(33.0, 34.0).all()
    assert sample_jobs["longitude"].between(-113.0, -111.0).all()


def test_sample_aoi_stays_within_ten_square_miles(
    sample_jobs: pd.DataFrame,
) -> None:
    points = [job.location for job in _domain_jobs(sample_jobs)]
    area = polygon_area_square_miles(bounding_polygon(points))

    assert area <= 10.0
    assert area <= DEFAULT_MAX_AOI_AREA_SQUARE_MILES


def test_committed_workday_is_feasible_without_synthetic_data(
    sample_jobs: pd.DataFrame,
) -> None:
    jobs = _domain_jobs(sample_jobs)
    neutral_profiles = {
        job.job_id: TemperatureProfile(
            job_id=job.job_id,
            points=(
                ConditionPoint(
                    minute_of_day=8 * 60,
                    temperature_c=30.0,
                    certainty=1.0,
                ),
                ConditionPoint(
                    minute_of_day=17 * 60,
                    temperature_c=30.0,
                    certainty=1.0,
                ),
            ),
        )
        for job in jobs
    }

    plans = compare_schedules(
        jobs,
        neutral_profiles,
        depot=DEPOT,
        uncertainty_penalty=0.0,
    )

    assert set(plans) == set(ScheduleStrategy)
    for plan in plans.values():
        assert len(plan.stops) == 6
        assert {stop.job_id for stop in plan.stops} == {job.job_id for job in jobs}
        assert plan.route_finish_minute <= 17 * 60
