"""
Connection health vs. operation health (`src/providers/resolve.py::_readiness`
and `describe()`).

The bug this closes: `GET /api/providers/selection` used to report one
boolean, `ready`, computed from "does a credential exist and does the adapter
say it is available". That is exactly the confusion Section 3 names --
Apollo's cheap health check proves the key is valid, not that organization or
people search is available on this account's plan, and Gemini's model-listing
check proves the key works, not that the SPECIFIC configured model does. A
single "Connected" boolean cannot say any of that; `status` can.

No network call happens anywhere in this file: `_readiness` is a pure
function over a `ProviderSpec` and an adapter, so these tests build both by
hand rather than resolving a real one.
"""

from __future__ import annotations

import pytest

from src.providers import registry
from src.providers.base import Capability, CredentialType
from src.providers.resolve import _readiness, describe


class _FakeAdapter:
    def __init__(self, available: bool = True) -> None:
        self._available = available

    def available(self) -> bool:
        return self._available


def _spec(**overrides) -> registry.ProviderSpec:
    base = dict(
        id="test_provider",
        display_name="Test Provider",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        connection_fields=(
            registry.ConnectionField(name="api_key", label="API key"),
        ),
        env_vars={"api_key": "TEST_PROVIDER_API_KEY"},
    )
    base.update(overrides)
    return registry.ProviderSpec(**base)


@pytest.fixture(autouse=True)
def _no_stray_key(monkeypatch):
    monkeypatch.delenv("TEST_PROVIDER_API_KEY", raising=False)


# --------------------------------------------------------------------------- #
# The four states
# --------------------------------------------------------------------------- #


def test_no_registry_entry_is_not_configured():
    status, message = _readiness(None, _FakeAdapter(), "t")
    assert status == "NOT_CONFIGURED"
    assert message


def test_a_credentialed_provider_with_no_key_is_not_configured():
    status, message = _readiness(_spec(), _FakeAdapter(), "t")
    assert status == "NOT_CONFIGURED"
    assert "Test Provider" in message


def test_a_credentialed_provider_with_a_key_is_ready(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "key")
    status, _ = _readiness(_spec(), _FakeAdapter(available=True), "t")
    assert status == "READY"


def test_a_key_that_cannot_actually_run_is_not_ready(monkeypatch):
    """
    Credentials resolve, but the adapter itself reports it cannot make a real
    call -- a structural problem, not a plan limitation.
    """
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "key")
    status, message = _readiness(_spec(), _FakeAdapter(available=False), "t")
    assert status == "NOT_READY"
    assert "cannot run" in message.lower()


def test_an_unverified_operation_is_limited_not_ready(monkeypatch):
    """
    The exact Apollo/Gemini case: the connection test passed, but it did not
    exercise the operation this workspace actually needs.
    """
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "key")
    status, message = _readiness(
        _spec(operation_verified=False), _FakeAdapter(available=True), "t"
    )
    assert status == "LIMITED"
    assert "operation" in message.lower()
    assert "not" in message.lower()


def test_a_credential_free_provider_is_ready_with_nothing_connected():
    """OSM's whole point: no key, and that is a legitimate READY, not a gap."""
    status, _ = _readiness(
        _spec(credential_type=CredentialType.NONE), _FakeAdapter(available=True), "t"
    )
    assert status == "READY"


def test_a_credential_free_provider_that_cannot_run_is_still_not_ready():
    status, _ = _readiness(
        _spec(credential_type=CredentialType.NONE), _FakeAdapter(available=False), "t"
    )
    assert status == "NOT_READY"


# --------------------------------------------------------------------------- #
# The real registry: which providers are actually flagged
# --------------------------------------------------------------------------- #


def test_apollo_and_gemini_are_flagged_operation_unverified():
    """
    Locks in the two examples Section 3 names by name -- a future edit that
    quietly removes the flag (because "the connection test passes, so what's
    the problem") regresses exactly the bug this file exists to catch.
    """
    assert registry.get("apollo").operation_verified is False
    assert registry.get("gemini").operation_verified is False


def test_apollo_credentialed_is_limited_not_ready(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "fake-key")
    status, message = _readiness(registry.get("apollo"), _FakeAdapter(), "t")
    assert status == "LIMITED"
    assert "Apollo" in message


def test_gemini_credentialed_is_limited_not_ready(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")
    status, message = _readiness(registry.get("gemini"), _FakeAdapter(), "t")
    assert status == "LIMITED"
    assert "Google Gemini" in message


def test_most_providers_are_not_flagged():
    """
    The flag is the exception, not the rule -- most connection tests genuinely
    exercise the operation the pipeline uses (Groq/OpenAI/Anthropic/DeepSeek
    list the models they actually complete with; Hunter/DeepL/Brevo read the
    real account; Gmail/SMTP/IMAP log in for real). Flagging everything
    "to be safe" would make LIMITED meaningless.
    """
    unverified = {spec.id for spec in registry.PROVIDERS if not spec.operation_verified}
    assert unverified == {"apollo", "gemini"}


# --------------------------------------------------------------------------- #
# describe(): the whole thing, end to end, for a provider that needs no key
# --------------------------------------------------------------------------- #


def test_describe_reports_osm_ready_with_nothing_connected():
    """
    No tenant config, no credentials anywhere -- OSM still needs none, so its
    capability must come back READY, not NOT_CONFIGURED.
    """
    result = describe(None)
    assert result["discovery_local"]["primary"] == "osm"
    assert result["discovery_local"]["status"] == "READY"
    assert result["discovery_local"]["ready"] is True


def test_describe_status_and_ready_boolean_never_disagree():
    """`ready` is derived from `status`, not computed separately -- they must
    never diverge for any capability describe() reports."""
    result = describe(None)
    for payload in result.values():
        assert payload["ready"] == (payload["status"] == "READY")
        assert payload["status"] in (
            "READY", "LIMITED", "NOT_READY", "NOT_CONFIGURED",
        )
