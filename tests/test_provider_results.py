"""
The shared provider result/error layer (`src/providers/results.py`).

This is the module the "0 Found" bug is actually about: `[]` used to be able
to mean five different things, and a dashboard could not tell a rejected key
from a quiet region. These tests cover the normalization and retry mechanics
in isolation, so every integration that adopts this layer inherits behaviour
that is already proven rather than re-testing HTTP status mapping per vendor.
"""

from __future__ import annotations

import pytest

from src.providers.results import (
    ErrorCode,
    ProviderError,
    ProviderResult,
    ProviderStatus,
    call_with_retries,
    classify_exception,
    classify_http_status,
)


# --------------------------------------------------------------------------- #
# ProviderResult / ProviderError basics
# --------------------------------------------------------------------------- #


def test_success_with_data_is_success_with_results():
    result = ProviderResult.success("osm", "discover_local", ["a business"])
    assert result.status is ProviderStatus.SUCCESS_WITH_RESULTS
    assert result.ok is True
    assert result.error is None


def test_success_with_no_data_is_success_empty_not_an_error():
    """
    The rule this whole module exists to enforce: a provider that ran and
    found nothing is not a failure.
    """
    result = ProviderResult.success("osm", "discover_local", [])
    assert result.status is ProviderStatus.SUCCESS_EMPTY
    assert result.ok is True
    assert result.error is None


def test_failure_is_never_confusable_with_success_empty():
    error = ProviderError(ErrorCode.PERMISSION_DENIED, "apollo", "discover_contacts", message="403")
    result = ProviderResult.failure(error, data=[])
    assert result.status is ProviderStatus.ERROR
    assert result.ok is False
    assert result.error is error
    # The data shape callers expect (a list) is still there for convenience,
    # but `status`/`ok` is what a caller must actually branch on.
    assert result.data == []


def test_a_provider_error_fills_in_safe_defaults():
    error = ProviderError(ErrorCode.RATE_LIMITED, "hunter", "find_email", message="429 from hunter")
    assert error.user_message  # never blank -- always something showable
    assert error.user_action
    assert "hunter" in str(error)


def test_to_dict_never_includes_the_log_facing_message():
    """
    `message` may echo a raw provider response body; `to_dict()` is what
    crosses into the run record and the dashboard, and must not carry it.
    """
    error = ProviderError(
        ErrorCode.AUTH_ERROR, "apollo", "discover_contacts",
        message="401: {\"error\": \"key sk-should-never-appear-in-the-ui\"}",
        user_message="That connection's key was rejected.",
    )
    payload = error.to_dict()
    assert "message" not in payload
    assert "sk-should-never-appear-in-the-ui" not in str(payload)
    assert payload["code"] == "auth_error"
    assert payload["user_message"] == "That connection's key was rejected."


# --------------------------------------------------------------------------- #
# classify_http_status
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status, expected_code, expected_retryable",
    [
        (401, ErrorCode.AUTH_ERROR, False),
        (403, ErrorCode.PERMISSION_DENIED, False),
        (404, ErrorCode.ENDPOINT_NOT_FOUND, False),
        (400, ErrorCode.INVALID_REQUEST, False),
        (422, ErrorCode.INVALID_REQUEST, False),
        (429, ErrorCode.RATE_LIMITED, True),
        (500, ErrorCode.PROVIDER_UNAVAILABLE, True),
        (503, ErrorCode.PROVIDER_UNAVAILABLE, True),
        (418, ErrorCode.UNKNOWN_PROVIDER_ERROR, False),
    ],
)
def test_http_status_classification(status, expected_code, expected_retryable):
    error = classify_http_status(status, "body", provider="x", operation="op")
    assert error.code is expected_code
    assert error.retryable is expected_retryable
    assert error.http_status == status


def test_429_carries_the_retry_after_it_was_given():
    error = classify_http_status(429, "", provider="x", operation="op", retry_after=12.5)
    assert error.retry_after == 12.5


def test_a_rejected_key_never_endlessly_retries():
    """Never used to describe 401/403/404/invalid-request -- only real
    transient conditions are retryable."""
    for status in (400, 401, 403, 404, 422):
        assert classify_http_status(status, "", provider="x", operation="op").retryable is False


# --------------------------------------------------------------------------- #
# classify_exception
# --------------------------------------------------------------------------- #


def test_a_timeout_is_retryable():
    error = classify_exception(TimeoutError("timed out"), provider="x", operation="op")
    assert error.code is ErrorCode.TIMEOUT
    assert error.retryable is True


def test_a_connection_failure_is_retryable():
    error = classify_exception(ConnectionError("refused"), provider="x", operation="op")
    assert error.code is ErrorCode.NETWORK_ERROR
    assert error.retryable is True


def test_a_malformed_response_is_not_retryable():
    error = classify_exception(ValueError("Expecting value: line 1"), provider="x", operation="op")
    assert error.code is ErrorCode.BAD_RESPONSE
    assert error.retryable is False


def test_an_unrecognised_exception_is_unknown_not_misclassified():
    error = classify_exception(RuntimeError("something odd"), provider="x", operation="op")
    assert error.code is ErrorCode.UNKNOWN_PROVIDER_ERROR


# --------------------------------------------------------------------------- #
# call_with_retries
# --------------------------------------------------------------------------- #


def test_a_successful_call_never_sleeps():
    sleeps: list[float] = []
    assert call_with_retries(lambda: "ok", sleep=sleeps.append) == "ok"
    assert sleeps == []


def test_a_non_retryable_error_is_raised_immediately_without_sleeping():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fail():
        calls["n"] += 1
        raise ProviderError(ErrorCode.PERMISSION_DENIED, "x", "op", message="403")

    with pytest.raises(ProviderError) as caught:
        call_with_retries(fail, sleep=sleeps.append)
    assert caught.value.code is ErrorCode.PERMISSION_DENIED
    assert calls["n"] == 1
    assert sleeps == []


def test_a_retryable_error_is_retried_and_can_then_succeed():
    sleeps: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ProviderError(
                ErrorCode.PROVIDER_UNAVAILABLE, "x", "op", message="503", retryable=True,
            )
        return "recovered"

    assert call_with_retries(flaky, sleep=sleeps.append) == "recovered"
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_retry_after_is_honoured_over_the_backoff_schedule():
    sleeps: list[float] = []
    calls = {"n": 0}

    def rate_limited():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ProviderError(
                ErrorCode.RATE_LIMITED, "x", "op", message="429",
                retryable=True, retry_after=3.0,
            )
        return "ok"

    call_with_retries(rate_limited, sleep=sleeps.append)
    assert sleeps == [3.0]


def test_exhausting_every_attempt_raises_the_final_error():
    sleeps: list[float] = []

    def always_fails():
        raise ProviderError(
            ErrorCode.RATE_LIMITED, "x", "op", message="429", retryable=True,
        )

    with pytest.raises(ProviderError):
        call_with_retries(always_fails, max_attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2  # slept between attempts 1->2 and 2->3, then gave up


# --------------------------------------------------------------------------- #
# Capability routing: a custom endpoint gets the same treatment, not a
# separate one -- proof this layer is not just Apollo and OSM in disguise.
# --------------------------------------------------------------------------- #


def test_a_custom_discovery_endpoint_reports_its_failure_not_an_empty_list(monkeypatch):
    from src.providers.base import DiscoveryRequest
    from src.providers import custom as custom_module
    from src.providers.custom import CustomConfig, CustomDiscovery, CustomEndpointError

    config = CustomConfig(base_url="https://example.com/hook", capability="discovery_local")
    adapter = CustomDiscovery(config, "local_business")

    def explode(*args, **kwargs):
        raise CustomEndpointError("example.com answered 403. plan does not allow this")

    monkeypatch.setattr(custom_module, "call", explode)
    result = adapter.find(DiscoveryRequest(niche_id="n", region="UK"))

    assert not result.ok
    assert result.error.code is ErrorCode.PERMISSION_DENIED
    assert result.data == []


def test_a_custom_discovery_endpoint_with_a_malformed_body_is_bad_response(monkeypatch):
    from src.providers.base import DiscoveryRequest
    from src.providers import custom as custom_module
    from src.providers.custom import CustomConfig, CustomDiscovery

    config = CustomConfig(base_url="https://example.com/hook", capability="discovery_local")
    adapter = CustomDiscovery(config, "local_business")

    monkeypatch.setattr(custom_module, "call", lambda *a, **k: {"not_results": []})
    result = adapter.find(DiscoveryRequest(niche_id="n", region="UK"))

    assert not result.ok
    assert result.error.code is ErrorCode.BAD_RESPONSE


def test_a_custom_discovery_endpoint_that_answers_cleanly_succeeds(monkeypatch):
    from src.providers.base import DiscoveryRequest
    from src.providers import custom as custom_module
    from src.providers.custom import CustomConfig, CustomDiscovery

    config = CustomConfig(base_url="https://example.com/hook", capability="discovery_local")
    adapter = CustomDiscovery(config, "local_business")

    monkeypatch.setattr(
        custom_module, "call", lambda *a, **k: {"results": [{"company_name": "Acme"}]}
    )
    result = adapter.find(DiscoveryRequest(niche_id="n", region="UK"))

    assert result.ok
    assert result.data[0].company_name == "Acme"
