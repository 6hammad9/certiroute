"""Secret-handling and untrusted-response tests for the API boundary."""

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from certiroute.config import Settings
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.errors import FortyGuardHTTPError, FortyGuardProtocolError

SECRET = "super-secret-key-value"


def make_client(handler, **kwargs) -> FortyGuardClient:
    return FortyGuardClient(
        api_key=SECRET,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_api_key_is_sent_as_a_header_and_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"error": False, "data": {"status": "Completed", "result": {}}}
        )

    with make_client(handler) as client:
        client.get_activity("activity-123")

    request = seen[0]
    assert request.headers["api-key"] == SECRET
    assert SECRET not in str(request.url)
    assert SECRET not in request.url.query.decode()
    # Authorization/Bearer is not part of the documented contract.
    assert "authorization" not in {key.lower() for key in request.headers}


def test_blank_or_whitespace_api_keys_are_rejected() -> None:
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="API key is required"):
            FortyGuardClient(api_key=blank)


def test_secret_str_keys_are_accepted_without_exposing_the_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["api-key"] == SECRET
        return httpx.Response(
            200, json={"error": False, "data": {"status": "Completed", "result": {}}}
        )

    client = FortyGuardClient(
        api_key=SecretStr(SECRET), transport=httpx.MockTransport(handler)
    )
    with client:
        client.get_activity("activity-123")

    assert SECRET not in repr(client)


def test_api_error_messages_do_not_leak_the_key_and_are_length_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile or careless upstream echoing a huge body must not become an
        # unbounded log line or user-facing message.
        return httpx.Response(400, json={"message": "x" * 5000})

    with make_client(handler) as client:
        with pytest.raises(FortyGuardHTTPError) as error:
            client.get_activity("activity-123")

    message = str(error.value)
    assert SECRET not in message
    assert len(message) < 1000


def test_non_json_error_bodies_do_not_raise_unexpected_exceptions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"<html>gateway down</html>")

    with make_client(handler) as client:
        with pytest.raises(FortyGuardHTTPError) as error:
            client.get_activity("activity-123")

    assert error.value.status_code == 503


def test_non_json_success_bodies_are_reported_as_protocol_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    with make_client(handler) as client:
        with pytest.raises(FortyGuardProtocolError):
            client.get_activity("activity-123")


def test_json_arrays_where_objects_are_documented_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    with make_client(handler) as client:
        with pytest.raises(FortyGuardProtocolError):
            client.get_activity("activity-123")


def test_network_failures_are_wrapped_without_exposing_request_internals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler) as client:
        with pytest.raises(FortyGuardHTTPError) as error:
            client.get_activity("activity-123")

    assert SECRET not in str(error.value)


def test_settings_never_render_the_secret_in_repr_or_str() -> None:
    settings = Settings(
        fortyguard_api_key=SecretStr(SECRET),
        _env_file=None,
    )

    assert settings.fortyguard_api_key.get_secret_value() == SECRET
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in settings.model_dump_json()


def test_settings_reject_an_empty_api_key() -> None:
    with pytest.raises(ValidationError):
        Settings(fortyguard_api_key=SecretStr(""), _env_file=None)


def test_invalid_activity_ids_are_rejected_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for a blank activity id")

    with make_client(handler) as client:
        with pytest.raises(ValueError, match="activity_id is required"):
            client.get_activity("   ")
