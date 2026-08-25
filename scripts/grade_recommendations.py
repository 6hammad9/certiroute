"""Grade the shift-start recommendation on days the model never saw.

For each clean date this rebuilds the recommendation the committed model would
have made that morning - from the whole-day aggregate alone, never seeing an
hourly value - then replays every candidate start on the temperatures that day
actually produced.

    python scripts/grade_recommendations.py --area phoenix --live

Days the model trained or calibrated on are refused, not quietly included.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd

from certiroute.climatology import load_climatology
from certiroute.collection import HeatmapSnapshotStore
from certiroute.config import get_settings
from certiroute.daily_level import collect_daily_level
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.errors import FortyGuardProtocolError
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)
from certiroute.same_day import (
    LeakageError,
    build_same_day_plan,
    score_plan_against_measurements,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"
ARTIFACT_ROOT = PROJECT_ROOT / "data" / "climatology"
EVIDENCE_ROOT = PROJECT_ROOT / "data" / "evidence"

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


def load_jobs(relative_path: str, *, honour_windows: bool) -> list[Job]:
    """Load the committed jobs, optionally dropping their demo access windows.

    The bundled sample gives four of six sites access windows later than the
    usual start, which is realistic but leaves little room for an earlier
    shift. Grading the model's *timing* skill is clearer without them, so both
    views are available and both are reported.
    """

    frame = pd.read_csv(PROJECT_ROOT / relative_path)
    return [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
            earliest_start=(
                time.fromisoformat(row.earliest_start) if honour_windows else None
            ),
            latest_finish=(
                time.fromisoformat(row.latest_finish) if honour_windows else None
            ),
        )
        for row in frame.itertuples(index=False)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="phoenix", choices=sorted(AREA_JOB_SETS))
    parser.add_argument("--from", dest="start", default="2026-08-09")
    parser.add_argument("--to", dest="end", default=None)
    parser.add_argument("--start-hour", type=int, default=5)
    parser.add_argument("--end-hour", type=int, default=17)
    parser.add_argument("--baseline-start", default="08:00")
    parser.add_argument(
        "--honour-windows",
        action="store_true",
        help="Keep the sample's demo access windows instead of ignoring them.",
    )
    parser.add_argument(
        "--day-ahead",
        action="store_true",
        help="Plan each day from the previous day's reading, as the "
        "evening before would have to.",
    )
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()

    jobs = load_jobs(AREA_JOB_SETS[args.area], honour_windows=args.honour_windows)
    depot = GeoPoint(
        latitude=jobs[0].location.latitude,
        longitude=jobs[0].location.longitude,
    )
    polygon = bounding_polygon(job.location for job in jobs)
    model = load_climatology(args.area, root=ARTIFACT_ROOT)
    store = HeatmapSnapshotStore(CACHE_PATH)
    settings = get_settings()

    baseline = time.fromisoformat(args.baseline_start)
    shift_end = time(args.end_hour)
    candidates = tuple(time(hour) for hour in range(args.start_hour, baseline.hour + 1))
    hours = tuple(time(hour) for hour in range(args.start_hour, args.end_hour + 1))
    seen = set(model.training_dates) | set(model.evaluation.holdout_dates)

    print(
        f"Model     : {model.label}, trained {len(model.training_dates)} days, "
        f"held-out MAE {model.evaluation.mean_absolute_error_c:.2f} C"
    )
    print(f"Interval  : from {len(model.evaluation.day_scores_c)} held-out days")
    print(
        f"Baseline  : {baseline:%H:%M}   candidates "
        f"{candidates[0]:%H:%M}-{candidates[-1]:%H:%M}"
    )
    print(f"Windows   : {'honoured' if args.honour_windows else 'ignored (demo)'}\n")

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(args.start)
    span = [start + timedelta(days=n) for n in range((end - start).days + 1)]

    client = (
        FortyGuardClient(
            api_key=settings.fortyguard_api_key,
            base_url=settings.fortyguard_api_base_url,
            timeout_seconds=settings.fortyguard_timeout_seconds,
        )
        if args.live
        else None
    )
    results = []
    graded: list[tuple[date, object]] = []
    try:
        for target in span:
            if target in seen:
                continue
            requests = build_profile_requests(
                jobs,
                target_date=target,
                sample_times=hours,
                granularity=model.granularity_m,
            )
            if plan_profile_collection(requests, store).new_task_count:
                continue
            try:
                measured = collect_real_temperature_batch(
                    jobs, requests, store, client=None, max_new_tasks=0
                ).profiles
            except FortyGuardProtocolError as exc:
                # A cached hour that came back with no tiles cannot be
                # graded, and must not be silently treated as a pass.
                print(f"  {target}  unusable measurements: {exc}")
                continue
            anchor = target - timedelta(days=1) if args.day_ahead else target
            try:
                reading = collect_daily_level(
                    jobs,
                    polygon,
                    store,
                    target_date=anchor,
                    granularity=model.granularity_m,
                    client=client,
                    poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
                    max_attempts=settings.fortyguard_max_poll_attempts,
                )
            except LookupError as exc:
                print(f"  {target}  no aggregate for {anchor}: {exc}")
                continue

            plan = build_same_day_plan(
                jobs,
                model,
                reading,
                depot=depot,
                baseline_start=baseline,
                candidate_starts=candidates,
                shift_end=shift_end,
                target_date=target,
                **SCHEDULER,
            )
            try:
                outcome = score_plan_against_measurements(
                    plan,
                    measured,
                    depot=depot,
                    candidate_starts=candidates,
                    shift_end=shift_end,
                    **SCHEDULER,
                )
            except LeakageError as exc:
                print(f"  {target}  refused: {exc}")
                continue

            realized = outcome.realized_reduction
            results.append(outcome)
            graded.append((target, outcome))
            verdict = (
                "optimal"
                if outcome.chose_the_best_start
                else ("helped" if outcome.helped else "WORSE")
            )
            print(
                f"  {target}  chose {outcome.recommended_start:%H:%M}  "
                f"best {outcome.realized_best_start:%H:%M}  "
                f"avoided {'   n/a' if realized is None else f'{realized:6.1%}'}  "
                f"regret {outcome.regret_units:6.1f}  {verdict}"
            )
    finally:
        if client is not None:
            client.close()

    if not results:
        raise SystemExit("\nNo clean day could be graded.")

    optimal = sum(1 for outcome in results if outcome.chose_the_best_start)
    helped = sum(1 for outcome in results if outcome.helped)
    reductions = [
        outcome.realized_reduction
        for outcome in results
        if outcome.realized_reduction is not None
    ]
    print(f"\nGraded {len(results)} clean day(s)")
    print(f"  picked the best start : {optimal}/{len(results)}")
    print(f"  beat the usual start  : {helped}/{len(results)}")
    if reductions:
        print(f"  mean exposure avoided : {sum(reductions) / len(reductions):.1%}")
    print(
        f"  worst regret          : "
        f"{max(outcome.regret_units for outcome in results):.1f} degree-hours"
    )

    # Committed so the claim can be audited without rerunning the API.
    suffix = "_day_ahead" if args.day_ahead else ""
    evidence_path = EVIDENCE_ROOT / f"recommendation_grades_{args.area}{suffix}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "area_id": args.area,
                "model": {
                    "trained_dates": [d.isoformat() for d in model.training_dates],
                    "calibration_dates": [
                        d.isoformat() for d in model.evaluation.holdout_dates
                    ],
                    "held_out_mae_c": model.evaluation.mean_absolute_error_c,
                    "unseen_site_mae_c": model.evaluation.unseen_site_mae_c,
                    "granularity_m": model.granularity_m,
                },
                "baseline_start": baseline.isoformat(),
                "candidate_starts": [c.isoformat() for c in candidates],
                "honoured_job_windows": args.honour_windows,
                "planned_the_evening_before": args.day_ahead,
                "graded_days": [
                    {
                        "date": day.isoformat(),
                        "recommended_start": o.recommended_start.isoformat(),
                        "best_start_in_hindsight": o.realized_best_start.isoformat(),
                        "realized_reduction": o.realized_reduction,
                        "regret_units": o.regret_units,
                        "chose_the_best_start": o.chose_the_best_start,
                        "helped": o.helped,
                    }
                    for day, o in graded
                ],
                "summary": {
                    "graded_day_count": len(results),
                    "picked_best_start": optimal,
                    "beat_the_usual_start": helped,
                    "mean_exposure_avoided": (
                        sum(reductions) / len(reductions) if reductions else None
                    ),
                    "worst_regret_units": max(o.regret_units for o in results),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\nWrote {evidence_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
