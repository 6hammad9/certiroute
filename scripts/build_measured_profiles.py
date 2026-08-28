"""Distil the heatmap cache down to the numbers the product actually reads.

The cache holds whole heatmaps and comes to 1.8 GB, which cannot travel with
the repository. What the product reads is the temperature at each work site,
so this walks every cached day and writes just those out - 82 KB for every day
and city collected.

    python scripts/build_measured_profiles.py

A deployed instance then reviews and grades finished days with no API call,
which is what makes a live demo link possible when one request costs thousands
of credits.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, time, timedelta
from pathlib import Path

import pandas as pd

from certiroute.collection import HeatmapSnapshotStore
from certiroute.domain import GeoPoint, Job
from certiroute.measured import DEFAULT_PROFILE_PATH, build_payload
from certiroute.optimization import TemperatureProfile
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"

AREA_JOB_SETS = {
    "phoenix": "data/sample/phoenix_jobs.csv",
    "houston": "data/sample/houston_jobs.csv",
    "miami": "data/sample/miami_jobs.csv",
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-07-29")
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--start-hour", type=int, default=5)
    parser.add_argument("--end-hour", type=int, default=17)
    parser.add_argument("--granularity", type=int, default=60, choices=(60, 80, 100))
    args = parser.parse_args()

    store = HeatmapSnapshotStore(CACHE_PATH)
    hours = tuple(time(hour) for hour in range(args.start_hour, args.end_hour + 1))
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    span = [start + timedelta(days=n) for n in range((end - start).days + 1)]

    collected: dict[str, dict[date, dict[str, TemperatureProfile]]] = {}
    for area_id, job_path in AREA_JOB_SETS.items():
        jobs = load_jobs(job_path)
        days: dict[date, dict[str, TemperatureProfile]] = {}
        for target in span:
            requests = build_profile_requests(
                jobs,
                target_date=target,
                sample_times=hours,
                granularity=args.granularity,
            )
            try:
                if plan_profile_collection(requests, store).new_task_count:
                    continue
                days[target] = collect_real_temperature_batch(
                    jobs, requests, store, client=None, max_new_tasks=0
                ).profiles
            except Exception as exc:  # noqa: BLE001 - a bad day is skipped, not fatal
                print(f"  {area_id} {target}: skipped ({type(exc).__name__})")
        if days:
            collected[area_id] = days
        print(f"{area_id:9} {len(days):2d} complete day(s)")

    if not collected:
        raise SystemExit("\nNo complete day was found in the cache.")

    path = PROJECT_ROOT / DEFAULT_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_payload(collected), indent=0, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(len(days) for days in collected.values())
    size = path.stat().st_size
    print(
        f"\nWrote {path.relative_to(PROJECT_ROOT)}: {total} day(s) across "
        f"{len(collected)} area(s), {size / 1024:.0f} KB"
    )


if __name__ == "__main__":
    main()
