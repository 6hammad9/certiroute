"""Collect the exact FortyGuard heatmaps used by the Phoenix demo schedule."""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, date, datetime, time
from pathlib import Path

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
JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"


def load_jobs() -> list[Job]:
    """Load the committed job scenario without importing the Streamlit app."""

    with JOBS_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        Job(
            job_id=row["job_id"],
            name=row["name"],
            location=GeoPoint(
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            ),
            duration_minutes=int(row["duration_minutes"]),
            priority=int(row["priority"]),
            earliest_start=time.fromisoformat(row["earliest_start"]),
            latest_finish=time.fromisoformat(row["latest_finish"]),
        )
        for row in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or explicitly collect the three historical heatmaps used by "
            "CertiRoute's real-data Phoenix replay."
        )
    )
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 7, 15))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Authorize network submission after checking --max-new-tasks.",
    )
    parser.add_argument(
        "--max-new-tasks",
        type=int,
        default=0,
        help="Hard cap on new credit-consuming tasks; defaults to zero.",
    )
    parser.add_argument(
        "--verify-status",
        action="store_true",
        help=(
            "Use read-only status GETs to compare cached results with FortyGuard. "
            "This does not submit new heatmap tasks."
        ),
    )
    args = parser.parse_args()
    if args.max_new_tasks < 0:
        parser.error("--max-new-tasks cannot be negative")

    jobs = load_jobs()
    requests = build_profile_requests(jobs, target_date=args.date)
    store = HeatmapSnapshotStore(CACHE_PATH)
    now = datetime.now(UTC)
    plan = plan_profile_collection(requests, store, now_utc=now)
    print(
        f"Plan: {plan.request_count} exact samples; "
        f"{plan.cache_hit_count} cached; {plan.new_task_count} new tasks."
    )
    if not args.live:
        if args.verify_status:
            parser.error("--verify-status also requires --live")
        print("Dry run only. Pass --live with an adequate --max-new-tasks cap.")
        return
    if plan.new_task_count > args.max_new_tasks:
        parser.error(
            f"{plan.new_task_count} new tasks exceed --max-new-tasks="
            f"{args.max_new_tasks}"
        )

    settings = get_settings()
    client = None
    try:
        if plan.new_task_count:
            client = FortyGuardClient(
                api_key=settings.fortyguard_api_key,
                base_url=settings.fortyguard_api_base_url,
                timeout_seconds=settings.fortyguard_timeout_seconds,
            )
        batch = collect_real_temperature_batch(
            jobs,
            requests,
            store,
            client=client,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
            max_new_tasks=args.max_new_tasks,
            now_utc=now,
        )
    finally:
        if client is not None:
            client.close()

    print("Completed per-job temperature samples (°C):")
    for job_id, profile in batch.profiles.items():
        values = ", ".join(
            f"{point.minute_of_day // 60:02d}:{point.minute_of_day % 60:02d}="
            f"{point.temperature_c:.2f}"
            for point in profile.points
        )
        print(f"  {job_id}: {values}")
    print("Activity IDs:")
    for sample in batch.samples:
        retrieval = "cache" if sample.cache_hit else "fetched"
        print(
            f"  {sample.minute_of_day // 60:02d}:"
            f"{sample.minute_of_day % 60:02d} {sample.activity_id} ({retrieval})"
        )

    if args.verify_status:
        print("Verifying cached snapshots with read-only status requests:")
        with FortyGuardClient(
            api_key=settings.fortyguard_api_key,
            base_url=settings.fortyguard_api_base_url,
            timeout_seconds=settings.fortyguard_timeout_seconds,
        ) as verification_client:
            for sample in batch.samples:
                snapshot = store.get(sample.snapshot_id)
                if snapshot is None:
                    raise RuntimeError(
                        "cached snapshot disappeared during verification"
                    )
                live = verification_client.get_activity(sample.activity_id)
                if not live.is_complete or live.result != snapshot.raw_result:
                    raise RuntimeError(
                        f"cached snapshot did not match activity {sample.activity_id}"
                    )
                print(
                    f"  {sample.minute_of_day // 60:02d}:"
                    f"{sample.minute_of_day % 60:02d} completed and payload-equivalent"
                )


if __name__ == "__main__":
    main()
