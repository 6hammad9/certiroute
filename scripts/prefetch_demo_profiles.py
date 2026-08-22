"""Pre-fetch and permanently cache real FortyGuard hourly temperature profiles.

Historical snapshots are immutable, so the store never expires them. Running
this once makes the demo run on real per-tile API output with no network
dependency at demo time, which is safer in front of an audience than fetching
live and strictly better than falling back to synthetic curves.

    python scripts/prefetch_demo_profiles.py --live
"""

import argparse
from datetime import date, time
from pathlib import Path

import pandas as pd

from certiroute.collection import HeatmapSnapshotStore
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"


def load_jobs() -> list[Job]:
    """Load the committed demo jobs as domain models."""

    frame = pd.read_csv(SAMPLE_JOBS_PATH)
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default="2025-07-15",
        help="Historical target date (YYYY-MM-DD). Must be before today.",
    )
    parser.add_argument("--start-hour", type=int, default=8)
    parser.add_argument("--end-hour", type=int, default=17)
    parser.add_argument("--granularity", type=int, default=100, choices=(60, 80, 100))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that this consumes API credits.",
    )
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date)
    sample_times = tuple(
        time(hour) for hour in range(args.start_hour, args.end_hour + 1)
    )
    jobs = load_jobs()
    requests = build_profile_requests(
        jobs,
        target_date=target_date,
        sample_times=sample_times,
        granularity=args.granularity,
    )
    store = HeatmapSnapshotStore(CACHE_PATH)
    plan = plan_profile_collection(requests, store)

    print(f"Target date      : {target_date}")
    span = f"{args.start_hour:02d}:00-{args.end_hour:02d}:00"
    print(f"Hours requested  : {len(requests)} ({span})")
    print(f"Granularity      : {args.granularity} m")
    print(f"Already cached   : {plan.cache_hit_count}")
    print(f"New API tasks    : {plan.new_task_count}")
    print(f"Cache location   : {store.root}")

    if plan.new_task_count and not args.live:
        parser.error(
            f"{plan.new_task_count} new API tasks needed; pass --live to authorize"
        )

    settings = get_settings()
    client_context = (
        FortyGuardClient(
            api_key=settings.fortyguard_api_key,
            base_url=settings.fortyguard_api_base_url,
            timeout_seconds=settings.fortyguard_timeout_seconds,
        )
        if plan.new_task_count
        else None
    )

    try:
        batch = collect_real_temperature_batch(
            jobs,
            requests,
            store,
            client=client_context,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
            max_new_tasks=plan.new_task_count,
        )
    finally:
        if client_context is not None:
            client_context.close()

    print("\nCollected samples:")
    for sample in batch.samples:
        hours, minutes = divmod(sample.minute_of_day, 60)
        source = "cache" if sample.cache_hit else "API"
        print(f"  {hours:02d}:{minutes:02d}  {source:5}  {sample.activity_id}")

    print(f"\nProfiles built for {len(batch.profiles)} job sites.")
    for job_id, profile in sorted(batch.profiles.items()):
        readings = [
            f"{point.minute_of_day // 60:02d}:00={point.temperature_c:.1f}"
            for point in profile.points
        ]
        print(f"  {job_id}: {' '.join(readings)}")


if __name__ == "__main__":
    main()
