import json
from datetime import date, time

import httpx
import pytest

from certiroute.domain import GeoPoint
from certiroute.fortyguard import (
    FortyGuardClient,
    HeatmapRequest,
    SingleHourDateTime,
    bounding_polygon,
)
from certiroute.fortyguard.errors import (
    FortyGuardAOITooLarge,
    FortyGuardTaskFailed,
)


def heatmap_request() -> HeatmapRequest:
    return HeatmapRequest(
        polygon_aoi=bounding_polygon(
            [GeoPoint(latitude=33.44855, longitude=-112.07391)]
        ),
        date_time=SingleHourDateTime(
            start_date=date(2025, 7, 15), start_time=time(14, 0)
        ),
        granularity=100,
    )


def test_submit_and_poll_completed_activity() -> None:
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        assert request.headers["api-key"] == "test-secret"
        if request.method == "POST":
            assert request.url.path == "/v1/heatmap"
            payload = json.loads(request.content)
            assert payload["date_time"] == {
                "start_date": "2025-07-15",
                "start_time": "14:00",
                "filter_type": 1,
            }
            return httpx.Response(
                200,
                json={
                    "error": False,
                    "data": {"activity_id": "activity-123"},
                },
            )

        status_calls += 1
        status = "Processing" if status_calls == 1 else "Completed"
        result = None if status_calls == 1 else {"stats_data": {}}
        return httpx.Response(
            200,
            json={
                "error": False,
                "message": status,
                "data": {
                    "activity_id": "activity-123",
                    "status": status,
                    "result": result,
                },
            },
        )

    with FortyGuardClient(
        api_key="test-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        activity_id = client.submit_heatmap(heatmap_request())
        result = client.wait_for_activity(
            activity_id,
            poll_interval_seconds=0.001,
            max_attempts=3,
            sleep=lambda _: None,
        )

    assert activity_id == "activity-123"
    assert result == {"stats_data": {}}
    assert status_calls == 2


def test_failed_activity_is_terminal() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": False,
                "message": "Failed",
                "data": {
                    "activity_id": "activity-123",
                    "status": "Failed",
                },
            },
        )

    with FortyGuardClient(
        api_key="test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(FortyGuardTaskFailed):
            client.wait_for_activity(
                "activity-123",
                poll_interval_seconds=0.001,
                max_attempts=1,
                sleep=lambda _: None,
            )


def test_rate_limit_honors_retry_after_header() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "7"},
                json={"message": "Too many requests"},
            )
        return httpx.Response(
            200,
            json={
                "error": False,
                "message": "Completed",
                "data": {
                    "activity_id": "activity-123",
                    "status": "Completed",
                    "result": {"stats_data": {}},
                },
            },
        )

    with FortyGuardClient(
        api_key="test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        result = client.wait_for_activity(
            "activity-123",
            poll_interval_seconds=5,
            max_attempts=2,
            sleep=delays.append,
        )

    assert result == {"stats_data": {}}
    assert delays == [7]


def test_submit_rejects_oversized_aoi_before_network_request() -> None:
    network_calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    oversized_request = HeatmapRequest(
        polygon_aoi=bounding_polygon(
            [
                GeoPoint(latitude=33.45, longitude=-112.07),
                GeoPoint(latitude=33.50, longitude=-112.00),
            ]
        ),
        date_time=SingleHourDateTime(
            start_date=date(2025, 7, 15), start_time=time(14, 0)
        ),
    )

    with FortyGuardClient(
        api_key="test-secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(FortyGuardAOITooLarge):
            client.submit_heatmap(oversized_request)

    assert network_calls == 0


def test_submit_accepts_a_configured_larger_plan_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": False, "data": {"activity_id": "premium-request"}},
        )

    request = HeatmapRequest(
        polygon_aoi=bounding_polygon(
            [
                GeoPoint(latitude=33.45, longitude=-112.07),
                GeoPoint(latitude=33.50, longitude=-112.00),
            ]
        ),
        date_time=SingleHourDateTime(
            start_date=date(2025, 7, 15), start_time=time(14, 0)
        ),
    )

    with FortyGuardClient(
        api_key="test-secret",
        max_aoi_area_square_miles=20,
        transport=httpx.MockTransport(handler),
    ) as client:
        activity_id = client.submit_heatmap(request)

    assert activity_id == "premium-request"
