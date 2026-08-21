"""Errors raised by the FortyGuard integration boundary."""


class FortyGuardError(RuntimeError):
    """Base class for safe, application-facing FortyGuard errors."""


class FortyGuardAOITooLarge(FortyGuardError):
    """A heatmap AOI exceeds the configured plan allowance."""

    def __init__(self, area_square_miles: float, limit_square_miles: float) -> None:
        self.area_square_miles = area_square_miles
        self.limit_square_miles = limit_square_miles
        super().__init__(
            f"AOI area {area_square_miles:.3f} mi² exceeds the configured "
            f"{limit_square_miles:.3f} mi² plan limit"
        )


class FortyGuardHTTPError(FortyGuardError):
    """An HTTP response indicated that the API request failed."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.api_message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"FortyGuard request failed ({status_code}): {message}")


class FortyGuardProtocolError(FortyGuardError):
    """The service returned a response that did not match its public contract."""


class FortyGuardTaskFailed(FortyGuardError):
    """An asynchronous FortyGuard activity reached a failed terminal state."""


class FortyGuardTaskTimeout(FortyGuardError):
    """Polling ended before the activity reached a terminal state."""
