"""Distil the cached whole-day aggregates so a deployment can plan offline.

A plan needs one number per site: that day's whole-day level, which is what a
single FortyGuard request buys. The response it arrives in is a whole heatmap,
so those readings live inside the 1.8 GB snapshot cache and cannot travel with
the repository - and the aggregate is a same-day signal, so a day missed is a
day that can never be bought again.

Reading them out leaves six floats per day and city, small enough to commit.
A deployment then plans any shipped day end to end with no API call at all,
which is what lets someone open the live link on a date nobody anticipated and
still see the product work.

    python scripts/build_measured_levels.py

The levels are carried across unchanged. Deriving them instead from the hourly
measurements would be wrong: the mean of 05:00-17:00 runs 0.74 C warmer than
the aggregate, and the model's offsets were learned against the aggregate. A
level defined differently at serving time than at training time is exactly the
skew this project has already been bitten by once.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from certiroute.collection import HeatmapSnapshotStore
from certiroute.daily_level import collect_clustered_daily_level
from certiroute.domain import GeoPoint, Job
from certiroute.measured import DEFAULT_LEVEL_PATH, build_level_payload

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
        )
        for row in frame.itertuples(index=False)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", default="2026-07-29")
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--granularity", type=int, default=60, choices=(60, 80, 100))
    args = parser.parse_args()

    store = HeatmapSnapshotStore(CACHE_PATH)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    span = [start + timedelta(days=n) for n in range((end - start).days + 1)]

    collected: dict[str, dict[date, dict[str, float]]] = {}
    for area_id, job_path in AREA_JOB_SETS.items():
        jobs = load_jobs(job_path)
        days: dict[date, dict[str, float]] = {}
        for target in span:
            try:
                # client=None keeps this strictly offline: a day absent from the
                # cache is skipped, never re-bought.
                reading = collect_clustered_daily_level(
                    jobs,
                    store,
                    target_date=target,
                    granularity=args.granularity,
                    client=None,
                )
            except Exception:  # noqa: BLE001 - a missing day is ordinary here
                continue
            days[target] = dict(reading.level_by_job)
        if days:
            collected[area_id] = days
        print(f"{area_id:9} {len(days):2d} day(s) with a cached aggregate")

    if not collected:
        raise SystemExit("\nNo cached whole-day aggregate was found.")

    path = PROJECT_ROOT / DEFAULT_LEVEL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_level_payload(collected), indent=0, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    total = sum(len(days) for days in collected.values())
    print(
        f"\nWrote {path.relative_to(PROJECT_ROOT)}: {total} day(s) across "
        f"{len(collected)} area(s), {path.stat().st_size / 1024:.0f} KB"
    )


if __name__ == "__main__":
    main()
