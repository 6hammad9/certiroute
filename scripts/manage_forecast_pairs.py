"""Plan and audit fail-closed FortyGuard forecast/vendor-relative pairs.

Examples (all commands are network-free unless ``--live`` is present):

    python scripts/manage_forecast_pairs.py forecast requests.json
    python scripts/manage_forecast_pairs.py realize
    python scripts/manage_forecast_pairs.py status --json
    python scripts/manage_forecast_pairs.py report --format csv

A live command must include an explicit ``--max-new-tasks`` cap.  New
submissions currently fail closed because FortyGuard has not documented the
timezone semantics of heatmap ``start_date``/``start_time``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any

from certiroute.collection import HeatmapSnapshotStore
from certiroute.collection.pair_workflow import (
    DEFAULT_REALIZATION_SETTLING_DELAY,
    ForecastCollectionPlan,
    ForecastPairRepository,
    ForecastSemanticsUnverifiedError,
    RealizationCollectionPlan,
    apply_forecast_plan,
    apply_realization_plan,
    build_archive_status,
    build_vendor_relative_report,
    load_manifest,
    plan_forecast_collection,
    plan_realization_collection,
    require_verified_forecast_time_contract,
    status_as_dict,
)
from certiroute.config import get_settings
from certiroute.fortyguard import FortyGuardClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "raw" / "fortyguard_forecast_archive"
DEFAULT_SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=DEFAULT_ARCHIVE_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    forecast = subparsers.add_parser(
        "forecast", help="plan or collect future forecast vintages"
    )
    forecast.add_argument("manifest", type=Path)
    _add_live_arguments(forecast)

    realize = subparsers.add_parser(
        "realize", help="plan or attach matured same-vendor results"
    )
    _add_live_arguments(realize)
    _add_settling_delay(realize)

    status = subparsers.add_parser(
        "status", help="emit collection counts without network access"
    )
    status.add_argument("--json", action="store_true", dest="as_json")
    _add_settling_delay(status)

    report = subparsers.add_parser(
        "report", help="emit per-vintage vendor-relative audit rows"
    )
    report.add_argument("--format", choices=("json", "csv"), default="json")
    report.add_argument("--output", type=Path)
    _add_settling_delay(report)
    return parser


def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="apply cached work and authorize up to the explicit task cap",
    )
    parser.add_argument(
        "--max-new-tasks",
        type=int,
        default=None,
        help="required hard cap for --live; omitted in dry-run mode",
    )


def _add_settling_delay(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--settling-delay-minutes",
        type=int,
        default=int(DEFAULT_REALIZATION_SETTLING_DELAY.total_seconds() // 60),
        help="wait from target start; must be at least the full 60-minute window",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repository = ForecastPairRepository(args.archive_root)
        store = HeatmapSnapshotStore(args.snapshot_root)
        if args.command == "forecast":
            return _forecast(args, parser, repository, store)
        if args.command == "realize":
            return _realize(args, parser, repository, store)
        delay = _settling_delay(args, parser)
        if args.command == "status":
            status = build_archive_status(repository, settling_delay=delay)
            if args.as_json:
                print(json.dumps(status_as_dict(status), sort_keys=True))
            else:
                _print_status(status_as_dict(status))
            return 0
        rows = build_vendor_relative_report(repository, settling_delay=delay)
        _write_report(rows, output=args.output, output_format=args.format)
        return 0
    except ForecastSemanticsUnverifiedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _forecast(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
) -> int:
    manifest = load_manifest(args.manifest)
    plan = plan_forecast_collection(manifest, repository, store)
    _print_forecast_plan(plan)
    if not args.live:
        print("Dry run: no archive writes and no API submissions.")
        return 0
    cap = _live_cap(args, parser)
    _enforce_planned_cap(plan.new_task_count, cap)
    with _client_for(plan.new_task_count) as client:
        created = apply_forecast_plan(
            plan,
            repository,
            store,
            client=client,
            max_new_tasks=cap,
            poll_interval_seconds=(
                get_settings().fortyguard_poll_interval_seconds
                if client is not None
                else 2.0
            ),
            max_attempts=(
                get_settings().fortyguard_max_poll_attempts
                if client is not None
                else 60
            ),
        )
    print(f"Archived forecast vintages: {len(created)}")
    return 0


def _realize(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
) -> int:
    delay = _settling_delay(args, parser)
    plan = plan_realization_collection(
        repository,
        store,
        settling_delay=delay,
    )
    _print_realization_plan(plan)
    if not args.live:
        print("Dry run: no archive writes and no API submissions.")
        return 0
    cap = _live_cap(args, parser)
    _enforce_planned_cap(plan.new_task_count, cap)
    with _client_for(plan.new_task_count) as client:
        created = apply_realization_plan(
            plan,
            repository,
            store,
            client=client,
            max_new_tasks=cap,
            poll_interval_seconds=(
                get_settings().fortyguard_poll_interval_seconds
                if client is not None
                else 2.0
            ),
            max_attempts=(
                get_settings().fortyguard_max_poll_attempts
                if client is not None
                else 60
            ),
        )
    print(f"Attached vendor-relative realization vintages: {len(created)}")
    return 0


def _client_for(new_task_count: int):
    if not new_task_count:
        return nullcontext(None)
    # Fail with the contract-specific reason before configuration or a client
    # is touched.  The executor repeats this check immediately before use.
    require_verified_forecast_time_contract()
    settings = get_settings()
    return FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    )


def _live_cap(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.max_new_tasks is None:
        parser.error("--live requires an explicit --max-new-tasks N cap")
    if args.max_new_tasks < 0:
        parser.error("--max-new-tasks cannot be negative")
    return args.max_new_tasks


def _enforce_planned_cap(new_task_count: int, cap: int) -> None:
    if new_task_count > cap:
        raise ValueError(f"{new_task_count} new tasks exceed --max-new-tasks={cap}")


def _settling_delay(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> timedelta:
    if args.settling_delay_minutes < 60:
        parser.error("--settling-delay-minutes must be at least 60")
    return timedelta(minutes=args.settling_delay_minutes)


def _print_forecast_plan(plan: ForecastCollectionPlan) -> None:
    print(f"Future exact requests : {len(plan.items)}")
    print(f"Already archived      : {plan.already_archived_count}")
    print(f"Cached to archive     : {plan.cached_archive_count}")
    print(f"New API tasks         : {plan.new_task_count}")
    for item in plan.items:
        print(
            f"  {item.assumed_target_valid_at_utc.isoformat()}  "
            f"{item.action.value:16}  {item.request_fingerprint[:12]}"
        )


def _print_realization_plan(plan: RealizationCollectionPlan) -> None:
    print(f"Waiting forecasts     : {plan.waiting_forecast_count}")
    print(f"Already paired        : {plan.paired_forecast_count}")
    print(f"Mature forecasts due  : {plan.pending_forecast_count}")
    print(f"Exact cached requests : {plan.cached_request_count}")
    print(f"New API tasks         : {plan.new_task_count}")
    for item in plan.items:
        source = "exact-cache" if item.cached_snapshot else "new-task"
        print(
            f"  {item.earliest_allowed_at_utc.isoformat()}  {source:11}  "
            f"{len(item.forecasts)} vintage(s)  {item.request_fingerprint[:12]}"
        )


def _print_status(status: dict[str, Any]) -> None:
    print(f"Generated at UTC                 : {status['generated_at_utc']}")
    print(
        f"Forecast time contract          : {status['forecast_time_contract_status']}"
    )
    print(f"New API submissions enabled     : {status['new_api_submissions_enabled']}")
    print(f"Forecast vintages                : {status['total_forecast_vintages']}")
    print(f"Waiting for vendor realization   : {status['waiting_for_realization']}")
    print(f"Matured without vendor result   : {status['matured_without_realization']}")
    print(f"Paired forecast vintages         : {status['paired_forecast_vintages']}")
    print(
        "Vendor-relative result vintages : "
        f"{status['vendor_relative_realization_vintages']}"
    )


def _write_report(
    rows: tuple[dict[str, Any], ...],
    *,
    output: Path | None,
    output_format: str,
) -> None:
    handle_context = (
        output.open("w", encoding="utf-8", newline="")
        if output is not None
        else nullcontext(sys.stdout)
    )
    with handle_context as handle:
        if output_format == "json":
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        elif rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        else:
            handle.write("")


if __name__ == "__main__":
    raise SystemExit(main())
