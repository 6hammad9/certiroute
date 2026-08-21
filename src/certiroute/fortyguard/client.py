"""Synchronous, bounded client for FortyGuard's asynchronous task API."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

import httpx
from pydantic import SecretStr

from certiroute.fortyguard.errors import (
    FortyGuardHTTPError,
    FortyGuardProtocolError,
    FortyGuardTaskFailed,
    FortyGuardTaskTimeout,
)
from certiroute.fortyguard.geometry import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    validate_aoi_area,
)
from certiroute.fortyguard.schemas import HeatmapRequest

_COMPLETED_STATES = {"completed", "succeeded", "success"}
_FAILED_STATES = {"failed", "error"}
_PROCESSING_STATES = {"processing", "pending", "queued", "in_progress"}
_TRANSIENT_STATUS_CODES = {404, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ActivitySnapshot:
    """Normalized view of a FortyGuard activity response."""

    activity_id: str
    status: str
    result: Mapping[str, Any] | None
    message: str | None = None

    @property
    def normalized_status(self) -> str:
        return self.status.strip().lower().replace(" ", "_")

    @property
    def is_complete(self) -> bool:
        return self.normalized_status in _COMPLETED_STATES

    @property
    def is_failed(self) -> bool:
        return self.normalized_status in _FAILED_STATES

    @property
    def is_processing(self) -> bool:
        return self.normalized_status in _PROCESSING_STATES


class FortyGuardClient:
    """Client that never logs or embeds the API key in URLs."""

    def __init__(
        self,
        *,
        api_key: str | SecretStr,
        base_url: str = "https://api.fortyguard.com/v1",
        timeout_seconds: float = 30.0,
        max_aoi_area_square_miles: float = DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if isinstance(api_key, SecretStr):
            secret = api_key.get_secret_value()
        else:
            secret = api_key
        if not secret.strip():
            raise ValueError("FortyGuard API key is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not isfinite(max_aoi_area_square_miles) or max_aoi_area_square_miles <= 0:
            raise ValueError(
                "max_aoi_area_square_miles must be a finite value greater than zero"
            )

        normalized_base_url = f"{base_url.rstrip('/')}/"
        self._client = httpx.Client(
            base_url=normalized_base_url,
            headers={
                "api-key": secret,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
            transport=transport,
        )
        self._max_aoi_area_square_miles = max_aoi_area_square_miles

    def __enter__(self) -> FortyGuardClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def submit_heatmap(self, request: HeatmapRequest) -> str:
        """Submit one heatmap activity and return its identifier."""

        validate_aoi_area(
            request.polygon_aoi,
            max_area_square_miles=self._max_aoi_area_square_miles,
        )
        payload = request.model_dump(mode="json", exclude_none=True)
        response = self._request_json("POST", "heatmap", json=payload)
        data = self._require_mapping(response.get("data"), "data")
        activity_id = data.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id.strip():
            raise FortyGuardProtocolError(
                "Heatmap submission response did not contain data.activity_id"
            )
        return activity_id

    def get_activity(self, activity_id: str) -> ActivitySnapshot:
        """Retrieve and normalize one activity status response."""

        if not activity_id.strip():
            raise ValueError("activity_id is required")
        response = self._request_json("GET", f"status/{activity_id}")
        data = self._require_mapping(response.get("data"), "data")
        status = data.get("status")
        if not isinstance(status, str) or not status.strip():
            raise FortyGuardProtocolError(
                "Activity response did not contain data.status"
            )
        result_value = data.get("result")
        result = (
            self._require_mapping(result_value, "data.result")
            if result_value is not None
            else None
        )
        response_activity_id = data.get("activity_id", activity_id)
        return ActivitySnapshot(
            activity_id=str(response_activity_id),
            status=status,
            result=result,
            message=str(response.get("message")) if response.get("message") else None,
        )

    def wait_for_activity(
        self,
        activity_id: str,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Mapping[str, Any]:
        """Poll until completion, with strict attempt and interval bounds."""

        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")

        last_status = "unavailable"
        for attempt in range(max_attempts):
            next_delay = poll_interval_seconds
            try:
                snapshot = self.get_activity(activity_id)
            except FortyGuardHTTPError as exc:
                if exc.status_code not in _TRANSIENT_STATUS_CODES:
                    raise
                last_status = f"HTTP {exc.status_code}"
                if exc.retry_after_seconds is not None:
                    next_delay = max(next_delay, exc.retry_after_seconds)
            else:
                last_status = snapshot.status
                if snapshot.is_complete:
                    if snapshot.result is None:
                        raise FortyGuardProtocolError(
                            "Completed activity did not contain data.result"
                        )
                    return snapshot.result
                if snapshot.is_failed:
                    raise FortyGuardTaskFailed(
                        f"Activity {activity_id} failed: "
                        f"{snapshot.message or 'no failure reason supplied'}"
                    )
                if not snapshot.is_processing:
                    raise FortyGuardProtocolError(
                        f"Activity {activity_id} returned unknown status "
                        f"{snapshot.status!r}"
                    )

            if attempt < max_attempts - 1:
                sleep(next_delay)

        raise FortyGuardTaskTimeout(
            f"Activity {activity_id} did not complete after {max_attempts} "
            f"checks (last status: {last_status})"
        )

    def create_heatmap(
        self,
        request: HeatmapRequest,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ) -> tuple[str, Mapping[str, Any]]:
        """Submit and retrieve one heatmap using bounded polling."""

        activity_id = self.submit_heatmap(request)
        result = self.wait_for_activity(
            activity_id,
            poll_interval_seconds=poll_interval_seconds,
            max_attempts=max_attempts,
        )
        return activity_id, result

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise FortyGuardHTTPError(
                0, "network request could not be completed"
            ) from exc

        if response.is_error:
            message = self._safe_error_message(response)
            raise FortyGuardHTTPError(
                response.status_code,
                message,
                retry_after_seconds=self._retry_after_seconds(response),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise FortyGuardProtocolError("API response was not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise FortyGuardProtocolError("API response was not a JSON object")
        if payload.get("error") is True:
            message = payload.get("message")
            raise FortyGuardHTTPError(
                response.status_code,
                str(message) if message else "API reported an error",
            )
        return payload

    @staticmethod
    def _require_mapping(value: object, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise FortyGuardProtocolError(f"{field_name} was not a JSON object")
        return value

    @staticmethod
    def _safe_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or "request failed"
        if isinstance(payload, Mapping):
            for key in ("message", "detail", "error"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value[:500]
        return response.reason_phrase or "request failed"

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return max(seconds, 0.0)
