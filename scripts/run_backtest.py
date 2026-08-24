"""Turn the collected corpus into decision evidence, offline.

Reads ``data/evidence/reliability_corpus.json`` and runs the leakage-safe
rolling-origin backtest: Phoenix days calibrate, while a held-out Phoenix date
and two entirely unseen cities evaluate. No network access and no API credits.

    python scripts/run_backtest.py
    python scripts/run_backtest.py --json data/evidence/backtest_report.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, time
from pathlib import Path
from typing import Any

from certiroute.domain import GeoPoint, Job
from certiroute.evaluation import (
    BacktestHoldout,
    HistoricalRouteDay,
    run_rolling_origin_backtest,
)
from certiroute.optimization import ConditionPoint, TemperatureProfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = PROJECT_ROOT / "data" / "evidence" / "reliability_corpus.json"


def build_day(case: dict[str, Any], source_label: str) -> HistoricalRouteDay:
    """Convert one collected case into a backtest day."""

    jobs = tuple(
        Job(
            job_id=entry["job_id"],
            name=entry.get("name", entry["job_id"]),
            location=GeoPoint(latitude=entry["latitude"], longitude=entry["longitude"]),
            duration_minutes=entry["duration_minutes"],
            priority=entry["priority"],
            earliest_start=time.fromisoformat(entry["earliest_start"]),
            latest_finish=time.fromisoformat(entry["latest_finish"]),
        )
        for entry in case["jobs"]
    )
    profiles = tuple(
        TemperatureProfile(
            job_id=job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=point["minute_of_day"],
                    temperature_c=point["temperature_c"],
                    certainty=1.0,
                )
                for point in sorted(points, key=lambda item: item["minute_of_day"])
            ),
        )
        for job_id, points in sorted(case["profiles"].items())
    )
    service_date = date.fromisoformat(case["target_date"])
    return HistoricalRouteDay(
        case_id=f"{case['geography']}-{service_date.isoformat()}",
        geography=case["geography"],
        service_date=service_date,
        source_label=source_label,
        jobs=jobs,
        depot=jobs[0].location,
        realized_profiles=profiles,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--issuance-hour", type=int, default=8)
    parser.add_argument("--miscoverage", type=float, default=0.1)
    args = parser.parse_args()

    if not args.corpus.exists():
        print(f"No corpus at {args.corpus}.")
        print("Run: python scripts/collect_reliability_corpus.py --live ...")
        return 1

    payload = json.loads(args.corpus.read_text(encoding="utf-8"))
    cases = payload["cases"]
    days = [build_day(case, payload["source"]) for case in cases]

    held_dates = {
        date.fromisoformat(case["target_date"])
        for case in cases
        if case["role"] == "held_out_date"
    }
    held_geographies = {
        case["geography"] for case in cases if case["role"] == "held_out_geography"
    }
    holdout = BacktestHoldout(
        dates=frozenset(held_dates), geographies=frozenset(held_geographies)
    )

    print(f"Corpus            : {args.corpus}")
    print(f"Point forecast    : {payload['forecast_baseline']}")
    print(f"Independent truth : {payload['independent_ground_truth']}")
    print(f"Realization label : {payload['realization_label']}")
    print()
    for case in cases:
        print(
            f"  {case['role']:<20} {case['geography']:<9} "
            f"{case['target_date']}  {len(case['jobs'])} jobs"
        )
    print()
    print(f"Held-out dates      : {sorted(d.isoformat() for d in held_dates)}")
    print(f"Held-out geographies: {sorted(held_geographies)}")
    print()

    report = run_rolling_origin_backtest(
        days,
        holdout=holdout,
        issuance_minute=args.issuance_hour * 60,
        reliability_miscoverage=args.miscoverage,
    )

    aggregate = report.aggregate
    print("=" * 66)
    print("ROLLING-ORIGIN RESULT")
    print("=" * 66)
    print(f"Evaluated cases            : {aggregate.case_count}")
    print(
        f"Decision changed           : {aggregate.decision_change_count} "
        f"({aggregate.decision_change_rate:.0%})"
    )
    print(
        "Scenario route better on realized data: "
        f"{aggregate.scenario_route_realized_exposure_better_count}"
        f"/{aggregate.case_count}"
    )
    print(
        "Mean realized exposure delta: "
        f"{aggregate.mean_realized_exposure_delta_units:+.3f} degree-hours "
        "(negative favours the scenario route)"
    )
    print()
    print("Per case:")
    for case in report.cases:
        payload_case = case.model_dump()
        changed = payload_case.get("decision_changed")
        delta = payload_case.get("realized_exposure_delta_units")
        label = payload_case.get("case_id", "?")
        line = f"  {str(label):<26}"
        if changed is not None:
            line += f" decision_changed={str(changed):<5}"
        if isinstance(delta, (int, float)):
            line += f" realized_delta={delta:+7.3f}"
        print(line)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"\nWrote report: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
