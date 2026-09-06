"""
Where the settings service listens, and why.

The precedence is the whole point of these tests: `BACKEND_PORT` is this
project's own name for the port, `PORT` is what a hosting platform injects, and
getting the order wrong fails in a way that is invisible locally -- the service
starts, says "Application startup complete", and the platform's router points at
a socket nobody is listening on.
"""

from __future__ import annotations

import pytest

from backend.main import DEFAULT_PORT, resolve_bind


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every case from an environment that says nothing about binding."""
    for key in ("BACKEND_HOST", "BACKEND_PORT", "PORT", "BACKEND_RELOAD"):
        monkeypatch.delenv(key, raising=False)


def test_the_default_is_loopback_on_8000():
    """A developer who has configured nothing gets the documented default."""
    bind = resolve_bind()
    assert (bind.host, bind.port) == ("127.0.0.1", DEFAULT_PORT)
    assert bind.reload is False
    assert bind.warning == ""


def test_backend_port_moves_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BACKEND_PORT", "8123")
    assert resolve_bind().port == 8123


def test_the_platform_injected_port_is_honoured(monkeypatch: pytest.MonkeyPatch):
    """
    Render, Heroku, Fly and Cloud Run set PORT and route only to it.

    Ignoring it means a deploy that looks healthy in the logs and is
    unreachable from outside the container.
    """
    monkeypatch.setenv("PORT", "10000")
    assert resolve_bind().port == 10000


def test_backend_port_beats_the_injected_one(monkeypatch: pytest.MonkeyPatch):
    """The more specific variable wins -- somebody who set it meant it."""
    monkeypatch.setenv("BACKEND_PORT", "8123")
    monkeypatch.setenv("PORT", "10000")
    assert resolve_bind().port == 8123


def test_a_blank_backend_port_falls_through(monkeypatch: pytest.MonkeyPatch):
    """
    An empty variable is not a choice.

    Platforms and shell scripts export empty strings all the time; treating
    that as "port 0" or as a reason to ignore PORT would be a nasty way to
    lose an afternoon.
    """
    monkeypatch.setenv("BACKEND_PORT", "")
    monkeypatch.setenv("PORT", "10000")
    assert resolve_bind().port == 10000


def test_an_unparseable_port_does_not_crash_the_process(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BACKEND_PORT", "not-a-number")
    assert resolve_bind().port == DEFAULT_PORT


def test_loopback_under_an_injected_port_warns(monkeypatch: pytest.MonkeyPatch):
    """
    The deploy-shaped mistake, caught in words rather than by a silent 502.

    It is a warning and not an automatic switch to 0.0.0.0 on purpose: this
    service holds the encrypted credential store, and "expose on every
    interface" is not something to infer from an environment variable.
    """
    monkeypatch.setenv("PORT", "10000")
    bind = resolve_bind()
    assert bind.host == "127.0.0.1"
    assert "BACKEND_HOST=0.0.0.0" in bind.warning


def test_no_warning_once_the_host_is_reachable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    bind = resolve_bind()
    assert (bind.host, bind.port) == ("0.0.0.0", 10000)
    assert bind.warning == ""


def test_no_warning_without_an_injected_port(monkeypatch: pytest.MonkeyPatch):
    """Local development on loopback is the normal case, not a problem."""
    monkeypatch.setenv("BACKEND_PORT", "8000")
    assert resolve_bind().warning == ""


def test_reload_is_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch):
    assert resolve_bind().reload is False
    monkeypatch.setenv("BACKEND_RELOAD", "true")
    assert resolve_bind().reload is True
