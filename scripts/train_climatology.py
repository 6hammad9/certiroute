"""Train one operating area's diurnal model from cached FortyGuard snapshots.

Hourly history comes from the local snapshot store, so no hourly credits are
spent here. The only network calls are the whole-day aggregates - one per
training day - because that is the same quantity the running app reads for
today, and offsets must be measured against the anchor they will be used with.

    python scripts/train_climatology.py --area phoenix --live

The resulting artifact is committed. The app loads it and never trains.
"""

from __future__ import annotations

import argparse
from datetime import date, time, timedelta
from pathlib import Path

import pandas as pd

from certiroute.climatology import (
    TrainedArea,
    save_climatology,
    train_climatology_rolling,
)
from certiroute.collection import HeatmapSnapshotStore
from certiroute.config import get_settings
from certiroute.daily_level import collect_daily_level
from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import InsufficientHistoryError
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"
ARTIFACT_ROOT = PROJECT_ROOT / "data" / "climatology"

# Areas the project has collected history for. Each names the committed job
# set whose bounding box defines the trained AOI.
AREA_JOB_SETS = {
    "phoenix": ("Phoenix, Arizona", "data/sample/phoenix_jobs.csv"),
    "houston": ("Houston, Texas", "data/sample/houston_jobs.csv"),
    "miami": ("Miami, Florida", "data/sample/miami_jobs.csv"),
}


def load_jobs(relative_path: str) -> list[Job]:
    frame = pd.read_csv(PROJECT_ROOT / relative_path)
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


def candidate_dates(start: date, end: date) -> list[date]:
    span = (end - start).days
    if span < 0:
        raise SystemExit("--from must not be after --to")
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="phoenix", choices=sorted(AREA_JOB_SETS))
    parser.add_argument("--from", dest="start", default="2026-08-09")
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--start-hour", type=int, default=5)
    parser.add_argument("--end-hour", type=int, default=17)
    parser.add_argument("--granularity", type=int, default=60, choices=(60, 80, 100))
    parser.add_argument(
        "--min-train-days",
        type=int,
        default=4,
        help="Days of history required before a day can be scored.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Authorize the one whole-day aggregate call per training day.",
    )
    args = parser.parse_args()

    label, job_path = AREA_JOB_SETS[args.area]
    jobs = load_jobs(job_path)
    polygon = bounding_polygon(job.location for job in jobs)
    store = HeatmapSnapshotStore(CACHE_PATH)
    hours = tuple(time(hour) for hour in range(args.start_hour, args.end_hour + 1))
    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)

    print(f"Area        : {args.area} ({label})")
    print(f"Sites       : {len(jobs)}")
    print(f"Hours       : {args.start_hour:02d}:00-{args.end_hour:02d}:00")
    print(f"Granularity : {args.granularity} m\n")

    complete: list[tuple[date, dict]] = []
    for target in candidate_dates(date.fromisoformat(args.start), end):
        requests = build_profile_requests(
            jobs,
            target_date=target,
            sample_times=hours,
            granularity=args.granularity,
        )
        plan = plan_profile_collection(requests, store)
        if plan.new_task_count:
            print(f"  {target}  skipped: {plan.new_task_count} hour(s) not cached")
            continue
        batch = collect_real_temperature_batch(
            jobs, requests, store, client=None, max_new_tasks=0
        )
        complete.append((target, batch.profiles))
        print(f"  {target}  hourly profiles rebuilt from cache")

    if len(complete) <= args.min_train_days + 1:
        raise SystemExit(
            f"\n{len(complete)} complete day(s) available; training needs more "
            f"than {args.min_train_days + 1}. Backfill more days first."
        )

    settings = get_settings()
    client = (
        FortyGuardClient(
            api_key=settings.fortyguard_api_key,
            base_url=settings.fortyguard_api_base_url,
            timeout_seconds=settings.fortyguard_timeout_seconds,
        )
        if args.live
        else None
    )
    print("\nWhole-day aggregate anchors:")
    history = []
    try:
        for target, profiles in complete:
            try:
                reading = collect_daily_level(
                    jobs,
                    polygon,
                    store,
                    target_date=target,
                    granularity=args.granularity,
                    client=client,
                    poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
                    max_attempts=settings.fortyguard_max_poll_attempts,
                )
            except LookupError as exc:
                print(f"  {target}  unavailable: {exc}")
                continue
            source = "cache" if reading.cache_hit else "API"
            spread = max(reading.level_by_job.values()) - min(
                reading.level_by_job.values()
            )
            print(
                f"  {target}  area {reading.area_mean_c:6.2f} C  "
                f"site spread {spread:4.2f} C  ({source})"
            )
            # Per-site levels, not the area mean: the app anchors each site on
            # its own aggregate, so the offsets must be learned that way too.
            history.append((target, dict(reading.level_by_job), profiles))
    finally:
        if client is not None:
            client.close()

    try:
        model = train_climatology_rolling(
            history,
            area_id=args.area,
            label=label,
            granularity_m=args.granularity,
            min_train_days=args.min_train_days,
            trained_area=TrainedArea.covering(
                (job.location.latitude, job.location.longitude) for job in jobs
            ),
        )
    except InsufficientHistoryError as exc:
        raise SystemExit(f"\nTraining refused: {exc}") from exc

    evaluation = model.evaluation
    print(f"\nTrained on {len(model.training_dates)} day(s), held out "
          f"{len(evaluation.holdout_dates)}")
    print(f"  held-out MAE        : {evaluation.mean_absolute_error_c:.2f} C")
    print(f"  held-out worst      : {evaluation.worst_absolute_error_c:.2f} C")
    if evaluation.unseen_site_mae_c is not None:
        print(f"  unseen-site MAE     : {evaluation.unseen_site_mae_c:.2f} C")
    print(f"  day scores          : "
          f"{', '.join(f'{s:.2f}' for s in evaluation.day_scores_c)}")
    print(f"  supported interval  : "
          f"{(1 - evaluation.supported_miscoverage):.0%} coverage")

    path = save_climatology(model, root=ARTIFACT_ROOT)
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
