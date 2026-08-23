"""Collect a small, reproducible real-data corpus for reliability backtests.

The corpus is deliberately separate from the live forecast archive.  Every
value collected here is a completed historical FortyGuard heatmap value.  The
backtest can use the 08:00 value as a persistence forecast for later hours, but
that retrospective baseline must never be described as a stored FortyGuard
forecast vintage.

Run a dry plan first::

    python scripts/collect_reliability_corpus.py

Then execute exactly the displayed number of missing tasks::

    python scripts/collect_reliability_corpus.py --live --max-new-tasks N
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from certiroute.collection import HeatmapSnapshotStore
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.real_conditions import (
    RealTemperatureBatch,
    build_profile_requests,
    collect_real_temperature_batch_from_plan,
    plan_profile_collection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "evidence" / "reliability_corpus.json"
SAMPLE_TIMES = tuple(time(hour) for hour in range(8, 18))
GRANULARITY = 60


@dataclass(frozen=True)
class CorpusCase:
    """One city/date profile collection with a predeclared evaluation role."""

    geography: str
    target_date: date
    role: str


def _job(
    geography: str,
    index: int,
    latitude: float,
    longitude: float,
    *,
    duration_minutes: int,
    priority: int,
) -> Job:
    return Job(
        job_id=f"{geography.upper()}-{index}",
        name=f"{geography} evaluation site {index}",
        location=GeoPoint(latitude=latitude, longitude=longitude),
        duration_minutes=duration_minutes,
        priority=priority,
        earliest_start=time(8, 0),
        latest_finish=time(17, 0),
    )


def corpus_jobs() -> dict[str, list[Job]]:
    """Return public evaluation coordinates, not claims about real work orders."""

    return {
        "Phoenix": [
            _job("Phoenix", 1, 33.44855, -112.07391, duration_minutes=55, priority=5),
            _job("Phoenix", 2, 33.44530, -112.06670, duration_minutes=65, priority=3),
            _job("Phoenix", 3, 33.44965, -112.04760, duration_minutes=65, priority=4),
            _job("Phoenix", 4, 33.43520, -112.01030, duration_minutes=80, priority=5),
            _job("Phoenix", 5, 33.44590, -111.98560, duration_minutes=70, priority=3),
            _job("Phoenix", 6, 33.45080, -111.95650, duration_minutes=75, priority=2),
        ],
        "Houston": [
            _job("Houston", 1, 29.7587, -95.3677, duration_minutes=55, priority=5),
            _job("Houston", 2, 29.7520, -95.3570, duration_minutes=65, priority=3),
            _job("Houston", 3, 29.7463, -95.3470, duration_minutes=65, priority=4),
            _job("Houston", 4, 29.7385, -95.3375, duration_minutes=80, priority=5),
            _job("Houston", 5, 29.7305, -95.3265, duration_minutes=70, priority=3),
            _job("Houston", 6, 29.7225, -95.3155, duration_minutes=75, priority=2),
        ],
        # FortyGuard tile coverage has holes: a first pass over downtown Miami
        # returned 1078 tiles yet covered only one of six hand-picked points,
        # including one that sat inside the tile bounding box. These six are
        # centroids of tiles the API actually returned, so coverage is assured.
        "Miami": [
            _job("Miami", 1, 25.73854, -80.16457, duration_minutes=55, priority=5),
            _job("Miami", 2, 25.75538, -80.18896, duration_minutes=65, priority=3),
            _job("Miami", 3, 25.76509, -80.19069, duration_minutes=65, priority=4),
            _job("Miami", 4, 25.77088, -80.16553, duration_minutes=80, priority=5),
            _job("Miami", 5, 25.77415, -80.17149, duration_minutes=70, priority=3),
            _job("Miami", 6, 25.77697, -80.19480, duration_minutes=75, priority=2),
        ],
    }


def corpus_cases() -> tuple[CorpusCase, ...]:
    """Predeclare calibration, temporal holdout, and geographic holdouts."""

    return (
        CorpusCase("Phoenix", date(2026, 8, 18), "calibration"),
        CorpusCase("Phoenix", date(2026, 8, 19), "calibration"),
        CorpusCase("Phoenix", date(2026, 8, 20), "calibration"),
        CorpusCase("Phoenix", date(2026, 8, 21), "held_out_date"),
        CorpusCase("Houston", date(2026, 8, 21), "held_out_geography"),
        CorpusCase("Miami", date(2026, 8, 21), "held_out_geography"),
    )


def _batch_payload(
    case: CorpusCase,
    jobs: list[Job],
    batch: RealTemperatureBatch,
) -> dict[str, Any]:
    return {
        "geography": case.geography,
        "target_date": case.target_date.isoformat(),
        "role": case.role,
        "granularity_m": batch.granularity,
        "request_time_assumption": batch.request_time_assumption,
        "jobs": [
            {
                "job_id": job.job_id,
                "name": job.name,
                "latitude": job.location.latitude,
                "longitude": job.location.longitude,
                "duration_minutes": job.duration_minutes,
                "priority": job.priority,
                "earliest_start": job.earliest_start.isoformat(timespec="minutes"),
                "latest_finish": job.latest_finish.isoformat(timespec="minutes"),
            }
            for job in jobs
        ],
        "profiles": {
            job_id: [
                {
                    "minute_of_day": point.minute_of_day,
                    "temperature_c": point.temperature_c,
                }
                for point in profile.points
            ]
            for job_id, profile in batch.profiles.items()
        },
        "source_records": [
            {
                **asdict(sample),
                "collected_at_utc": sample.collected_at_utc.isoformat().replace(
                    "+00:00", "Z"
                ),
                "job_ids": list(sample.job_ids),
            }
            for sample in batch.samples
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Submit missing historical heatmap tasks after the explicit cap check.",
    )
    parser.add_argument(
        "--max-new-tasks",
        type=int,
        default=None,
        help="Required hard cap in live mode; use the count printed by dry-run.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_new_tasks is not None and args.max_new_tasks < 0:
        raise SystemExit("--max-new-tasks cannot be negative")
    if args.live and args.max_new_tasks is None:
        raise SystemExit("--live requires --max-new-tasks N")

    jobs_by_city = corpus_jobs()
    store = HeatmapSnapshotStore(RAW_SNAPSHOT_ROOT)
    planned: list[tuple[CorpusCase, list[Job], Any]] = []
    for case in corpus_cases():
        jobs = jobs_by_city[case.geography]
        requests = build_profile_requests(
            jobs,
            target_date=case.target_date,
            sample_times=SAMPLE_TIMES,
            granularity=GRANULARITY,
        )
        planned.append((case, jobs, plan_profile_collection(requests, store)))

    new_tasks = sum(plan.new_task_count for _, _, plan in planned)
    cache_hits = sum(plan.cache_hit_count for _, _, plan in planned)
    print(f"Cases           : {len(planned)}")
    print(f"Exact cache hits: {cache_hits}")
    print(f"New API tasks   : {new_tasks}")
    for case, _, plan in planned:
        print(
            f"  {case.role:22} {case.geography:8} {case.target_date}  "
            f"cached={plan.cache_hit_count:2} new={plan.new_task_count:2}"
        )
    if not args.live:
        print("Dry run: no API submissions and no derived corpus write.")
        return 0
    assert args.max_new_tasks is not None
    if new_tasks > args.max_new_tasks:
        raise SystemExit(
            f"planned {new_tasks} new tasks exceeds --max-new-tasks="
            f"{args.max_new_tasks}"
        )

    settings = get_settings()
    cases_payload: list[dict[str, Any]] = []
    with FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    ) as client:
        remaining_cap = args.max_new_tasks
        for case, jobs, plan in planned:
            batch = collect_real_temperature_batch_from_plan(
                jobs,
                plan,
                store,
                client=client if plan.new_task_count else None,
                poll_interval_seconds=min(
                    settings.fortyguard_poll_interval_seconds, 1.0
                ),
                max_attempts=settings.fortyguard_max_poll_attempts,
                max_new_tasks=remaining_cap,
            )
            remaining_cap -= plan.new_task_count
            cases_payload.append(_batch_payload(case, jobs, batch))
            print(f"Collected {case.geography} {case.target_date}")

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": "FortyGuard Temperature API historical single-hour heatmaps",
        "forecast_baseline": (
            "Retrospective 08:00 persistence; this is not an archived FortyGuard "
            "forecast and is used only to validate the decision pipeline."
        ),
        "independent_ground_truth": False,
        "realization_label": "later historical FortyGuard value",
        "cases": cases_payload,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote derived evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
