"""Freeze one real graded day into the landing page.

The landing page should show the product working before a visitor does
anything, and it must do that instantly - no API call, no dependence on a
snapshot cache that is not committed. So one day the model had never seen is
resolved here, once, and its playback is written to a small committed file.

    python scripts/build_showcase.py --area houston --date 2026-08-17

Nothing about this is a mock-up: the schedules come from the committed model
reading that day's real aggregate, and the day is one of those recorded in
data/evidence/recommendation_grades_*.json.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, time
from pathlib import Path

import pandas as pd

from certiroute.animation import (
    HEAT_COLOR,
    ROUTE_COLOR,
    PlaybackRun,
    build_playback_payload,
)
from certiroute.climatology import load_climatology
from certiroute.collection import HeatmapSnapshotStore
from certiroute.daily_level import collect_clustered_daily_level
from certiroute.domain import GeoPoint, Job
from certiroute.same_day import build_same_day_plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"
ARTIFACT_ROOT = PROJECT_ROOT / "data" / "climatology"
SHOWCASE_PATH = PROJECT_ROOT / "data" / "showcase" / "graded_day.json"

AREA_JOB_SETS = {
    "phoenix": "data/sample/phoenix_jobs.csv",
    "houston": "data/sample/houston_jobs.csv",
    "miami": "data/sample/miami_jobs.csv",
}

SCHEDULER = {
    "average_travel_speed_kph": 25.0,
    "reference_temperature_c": 27.0,
    "planning_threshold_c": 35.0,
    "uncertainty_penalty": 0.0,
    "heat_weight": 8.0,
}


def load_jobs(relative_path: str) -> list[Job]:
    """Load the sites without their demo access windows.

    The bundled windows pin four of six sites to mid-morning, which is
    realistic for that fictional job set but hides the timing effect the
    showcase exists to show.
    """

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
    parser.add_argument("--area", default="houston", choices=sorted(AREA_JOB_SETS))
    parser.add_argument("--date", default="2026-08-17")
    parser.add_argument("--baseline-start", default="08:00")
    parser.add_argument("--start-hour", type=int, default=5)
    parser.add_argument("--end-hour", type=int, default=17)
    args = parser.parse_args()

    target = date.fromisoformat(args.date)
    jobs = load_jobs(AREA_JOB_SETS[args.area])
    depot = GeoPoint(
        latitude=jobs[0].location.latitude,
        longitude=jobs[0].location.longitude,
    )
    model = load_climatology(args.area, root=ARTIFACT_ROOT)

    seen = set(model.training_dates) | set(model.evaluation.holdout_dates)
    if target in seen:
        raise SystemExit(
            f"{target} is a day this model was built from. The showcase must "
            "use a day it never saw, or it advertises memory as skill."
        )

    baseline = time.fromisoformat(args.baseline_start)
    candidates = tuple(time(hour) for hour in range(args.start_hour, baseline.hour + 1))
    store = HeatmapSnapshotStore(CACHE_PATH)
    reading = collect_clustered_daily_level(
        jobs, store, target_date=target, granularity=model.granularity_m, client=None
    )
    plan = build_same_day_plan(
        jobs,
        model,
        reading,
        depot=depot,
        baseline_start=baseline,
        candidate_starts=candidates,
        shift_end=time(args.end_hour),
        **SCHEDULER,
    )

    by_start = {
        option.shift_start: option.plan
        for option in plan.comparison.options
        if option.feasible and option.plan is not None
    }
    runs = [
        PlaybackRun(
            label=f"Recommended {plan.recommended_start:%H:%M}",
            plan=by_start[plan.recommended_start],
            color=ROUTE_COLOR,
            recommended=True,
        )
    ]
    if plan.changes_the_start and baseline in by_start:
        runs.append(
            PlaybackRun(
                label=f"Usual {baseline:%H:%M}",
                plan=by_start[baseline],
                color=HEAT_COLOR,
            )
        )

    payload = build_playback_payload(
        runs, plan.conservative_profiles, depot=depot, threshold_c=35.0
    )
    payload["showcase"] = {
        "area_id": args.area,
        "label": model.label,
        "date": target.isoformat(),
        "measured_level_c": round(reading.area_mean_c, 1),
        "recommended_start": plan.recommended_start.isoformat(timespec="minutes"),
        "baseline_start": baseline.isoformat(timespec="minutes"),
        "exposure_reduction": (
            None
            if plan.exposure_reduction is None
            else round(plan.exposure_reduction, 4)
        ),
        "interval_radius_c": round(plan.interval_radius_c, 2),
        "coverage": round(plan.coverage, 2),
        "site_count": len(jobs),
    }

    SHOWCASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHOWCASE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    detail = payload["showcase"]
    print(f"Area      : {detail['label']}")
    print(f"Day       : {detail['date']} (never trained or calibrated on)")
    print(f"Measured  : {detail['measured_level_c']} C across the area")
    print(f"Chose     : {detail['recommended_start']} vs usual "
          f"{detail['baseline_start']}")
    for run in payload["runs"]:
        print(f"  {run['label']:22} depart {run['depart']:4d}  home {run['finish']:4d}"
              f"  exposure {run['exposure']:7.1f}")
    print(f"\nWrote {SHOWCASE_PATH.relative_to(PROJECT_ROOT)} "
          f"({SHOWCASE_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
