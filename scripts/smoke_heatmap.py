"""Run one explicit, minimal live heatmap request for integration verification."""

import argparse
from datetime import date, time

from certiroute.config import get_settings
from certiroute.domain import GeoPoint
from certiroute.fortyguard import (
    FortyGuardClient,
    HeatmapRequest,
    SingleHourDateTime,
    bounding_polygon,
    extract_temperature_stats,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit one small historical FortyGuard heatmap."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that this request may consume credits.",
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("pass --live to acknowledge possible API credit usage")

    settings = get_settings()
    request = HeatmapRequest(
        polygon_aoi=bounding_polygon(
            [GeoPoint(latitude=33.44855, longitude=-112.07391)],
            margin_degrees=0.001,
        ),
        date_time=SingleHourDateTime(
            start_date=date(2025, 7, 15),
            start_time=time(14, 0),
        ),
        granularity=100,
    )

    with FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    ) as client:
        activity_id, result = client.create_heatmap(
            request,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
        )

    stats = extract_temperature_stats(result)
    print(f"Activity completed: {activity_id}")
    print(stats.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
