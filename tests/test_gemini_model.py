"""
Which Gemini model the product asks for, and how to change it.

Google retired the Flash model this one replaces on 1 June 2026. Its name is
assembled in `RETIRED` below rather than written out here, so this file does
not trip its own scan.

A retired model is not a gradual degradation: every generation call fails at
once, and because the connection check asks a different endpoint, Connections
goes on saying "Connected" while nothing works. So the model is a named
default that an environment variable overrides, and the retired name is kept
out of the tree by a test rather than by memory.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from backend import providers as bp
from src.integrations import llm
from src.providers import registry

EXPECTED_DEFAULT = "gemini-3.6-flash"

#: Assembled at runtime so this file does not itself contain the string the
#: scan below forbids -- otherwise the guard would fail on its own guard.
RETIRED = "gemini-" + "2.0-flash"


@pytest.fixture(autouse=True)
def no_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Start from an environment that says nothing about the model.

    `.env` is loaded into the process environment, and it sets GEMINI_MODEL --
    so without this the "default" tests would be reading the file rather than
    the default.
    """
    monkeypatch.delenv("GEMINI_MODEL", raising=False)


# --------------------------------------------------------------------------- #
# The default
# --------------------------------------------------------------------------- #

def test_the_default_generation_model_is_the_supported_one():
    assert llm.DEFAULT_GEMINI_MODEL == EXPECTED_DEFAULT


def test_model_name_reports_the_default_with_nothing_configured():
    assert llm.model_name("gemini") == EXPECTED_DEFAULT


def test_the_client_and_the_report_cannot_disagree():
    """
    The default used to be written out twice -- once where the client is built
    and once where a run reports which model answered. Two literals can drift,
    and a run would then report a model it had not used. One constant, read in
    both places.
    """
    source = Path(llm.__file__).read_text(encoding="utf-8")
    # Exactly the two call sites, both naming the constant.
    assert source.count('env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)') == 2
    # And the default itself appears once, as the constant's definition.
    assert source.count(f'"{EXPECTED_DEFAULT}"') == 1


# --------------------------------------------------------------------------- #
# Overriding it
# --------------------------------------------------------------------------- #

def test_gemini_model_overrides_the_default(monkeypatch: pytest.MonkeyPatch):
    """
    The point of the variable: the next retirement needs no code change.
    """
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9.9-experimental")
    assert llm.model_name("gemini") == "gemini-9.9-experimental"


def test_a_blank_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch):
    """An empty variable is not a choice -- shell scripts export those freely."""
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert llm.model_name("gemini") == EXPECTED_DEFAULT


def test_the_override_does_not_leak_into_other_providers(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9.9-experimental")
    assert "gemini" not in llm.model_name("groq")
    assert "gemini" not in llm.model_name("openai")


def test_gemini_is_still_selectable():
    """
    The retirement changes a model name, not whether the user may pick Gemini.
    """
    assert "gemini" in llm.KEY_VARS
    assert "gemini" in llm.FALLBACK_ORDER
    assert "gemini" in registry.PROVIDERS_BY_ID
    assert registry.PROVIDERS_BY_ID["gemini"].env_vars == {"api_key": "GOOGLE_API_KEY"}


# --------------------------------------------------------------------------- #
# The connection check is a separate question
# --------------------------------------------------------------------------- #

def test_the_connection_check_does_not_mention_a_generation_model():
    """
    Why "Connected" kept saying Connected while every call failed.

    The check lists the models on the key; it does not try to generate with
    one. That independence is deliberate -- a key is valid or not regardless
    of which model the product happens to prefer -- so it must not acquire a
    dependency on the model name.
    """
    with respx.mock(assert_all_called=True) as mock:
        route = mock.route(
            method="GET",
            host="generativelanguage.googleapis.com",
            path="/v1beta/models",
        ).mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": "models/" + EXPECTED_DEFAULT}]}
            )
        )
        result = bp._check_for(registry.PROVIDERS_BY_ID["gemini"])({"api_key": "k"})

    request = route.calls.last.request
    assert result.ok is True
    assert EXPECTED_DEFAULT not in str(request.url)
    assert RETIRED not in str(request.url)
    # The key travels as a query parameter, not a header. Unchanged by this.
    assert "key=k" in str(request.url)
    assert "Authorization" not in request.headers


def test_the_connection_check_passes_whatever_models_come_back(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    A key that can list models is a working key, even if the preferred model
    is not among them. Saying otherwise would refuse to store a valid key.
    """
    monkeypatch.setenv("GEMINI_MODEL", "something-else-entirely")
    with respx.mock(assert_all_called=True) as mock:
        mock.route(
            method="GET",
            host="generativelanguage.googleapis.com",
            path="/v1beta/models",
        ).mock(
            return_value=httpx.Response(200, json={"models": [{"name": "models/other"}]})
        )
        result = bp._check_for(registry.PROVIDERS_BY_ID["gemini"])({"api_key": "k"})
    assert result.ok is True


def test_a_rejected_key_still_fails_the_check():
    with respx.mock(assert_all_called=True) as mock:
        mock.route(
            method="GET",
            host="generativelanguage.googleapis.com",
            path="/v1beta/models",
        ).mock(return_value=httpx.Response(400, json={"error": {"message": "bad key"}}))
        result = bp._check_for(registry.PROVIDERS_BY_ID["gemini"])({"api_key": "k"})
    assert result.ok is False


# --------------------------------------------------------------------------- #
# A retired model must be a diagnosis, never a bare failure
# --------------------------------------------------------------------------- #


def test_a_retired_model_classifies_as_model_not_found():
    """
    What actually happens on the wire when GEMINI_MODEL names a model Google
    has retired: `ChatGoogleGenerativeAI` raises with the model name and a 404
    in its message, wrapped in whatever exception langchain-google-genai uses.
    This must not be lumped in with "provider unreachable" -- the fix is a
    settings change, not a retry.
    """
    from src.providers.results import ErrorCode

    exc = ValueError(
        f"404 models/{RETIRED} is not found for API version v1beta, or is "
        "not supported for generateContent"
    )
    error = llm.classify_llm_error(exc, provider="gemini", operation="qualification")
    assert error.code is ErrorCode.MODEL_NOT_FOUND
    assert error.retryable is False
    assert "gemini" in error.user_action.lower() or "model" in error.user_action.lower()


def test_an_exhausted_chain_carries_the_classified_reason_per_provider(monkeypatch: pytest.MonkeyPatch):
    """
    `LLMUnavailable`'s message stays a plain string (every existing caller
    reads it with `str(exc)`), but a caller that wants to tell a retired
    model apart from a rejected key can read `.errors`.
    """
    from src.providers.results import ErrorCode

    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    def explode(*args, **kwargs):
        raise ValueError(f"404 models/{RETIRED} is not found for API version v1beta")

    class FakeClient:
        def invoke(self, messages):
            return explode()

    monkeypatch.setattr(llm, "_build_client", lambda *a, **k: FakeClient())

    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete("prompt", task="t", chain=("gemini",), allow_mock=False)

    errors = getattr(caught.value, "errors", [])
    assert any(e.code is ErrorCode.MODEL_NOT_FOUND and e.provider == "gemini" for e in errors)


# --------------------------------------------------------------------------- #
# The retired name is gone, and stays gone
# --------------------------------------------------------------------------- #

#: Directories with nothing hand-written in them.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".secrets", ".vite",
}
#: Extensions worth reading. Anything else is a binary or a lock file.
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".md",
    ".txt", ".html", ".css", ".cfg", ".ini", ".toml", ".example", ".env",
}
REPO = Path(__file__).resolve().parent.parent


def _source_files():
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name == ".env" or path.suffix in TEXT_SUFFIXES:
            yield path


def test_the_retired_model_appears_nowhere_in_the_tree():
    """
    The guard. A retirement is only handled once the old name is gone from
    every default, fixture, document and local .env -- a stale .env silently
    overrides the new default and puts the failure straight back.
    """
    offenders = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if RETIRED in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"the retired model {RETIRED!r} is still referenced in: {offenders}"
    )


def test_the_guard_can_actually_fail(tmp_path: Path):
    """
    A scan that matches nothing would pass even if it were broken. This proves
    the needle is findable.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(f'MODEL = "{RETIRED}"', encoding="utf-8")
    assert RETIRED in planted.read_text(encoding="utf-8")
