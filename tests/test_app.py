from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_app_renders_without_exceptions(tmp_path, monkeypatch) -> None:
    app_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    monkeypatch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(tmp_path))
    app = AppTest.from_file(app_path)

    app.run(timeout=15)

    assert not app.exception
    assert any("CertiRoute" in str(block.value) for block in app.markdown)


def test_a_deployment_reviews_a_day_without_a_snapshot_cache(tmp_path) -> None:
    """A fresh deploy has no cache, and must not buy what it already ships.

    The snapshot cache is 1.8 GB of whole heatmaps and cannot travel with the
    repository. The measurements read out of it can, and do - so a review must
    resolve from those rather than costing thirteen live requests.
    """

    from datetime import date, time

    import pandas as pd

    from certiroute.collection import HeatmapSnapshotStore
    from certiroute.domain import GeoPoint, Job
    from certiroute.measured import DEFAULT_PROFILE_PATH, load_measured_profiles
    from certiroute.real_conditions import (
        build_profile_requests,
        plan_profile_collection,
    )

    root = Path(__file__).resolve().parents[1]
    frame = pd.read_csv(root / "data" / "sample" / "phoenix_jobs.csv")
    jobs = [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
        )
        for row in frame.itertuples(index=False)
    ]
    day = date(2026, 8, 21)
    requests = build_profile_requests(
        jobs,
        target_date=day,
        sample_times=tuple(time(hour) for hour in range(5, 18)),
        granularity=60,
    )

    # An empty cache would otherwise mean one live request per hour.
    plan = plan_profile_collection(requests, HeatmapSnapshotStore(tmp_path))
    assert plan.new_task_count == len(requests)

    profiles = load_measured_profiles("phoenix", day, path=root / DEFAULT_PROFILE_PATH)
    assert set(profiles) >= {job.job_id for job in jobs}
    for profile in profiles.values():
        assert len(profile.points) == 13
        assert all(0 < point.temperature_c < 60 for point in profile.points)
