"""
Credentials must not survive a trip through an error message.

A provider that rejects a call usually quotes the request back, and some
providers carry the key in the query string -- Gemini's REST calls use
`?key=...`. Those messages go to the log AND to the browser, so this has to be
airtight rather than careful.
"""

from __future__ import annotations

import pytest

from src.integrations.redact import scrub

# A deliberately fake key of each shape. None of these is real.
GOOGLE = "AIzaSyD1234567890abcdefghijklmnopqrs"
OPENAI = "sk-proj-abcdefghijklmnopqrstuvwxyz1234"
GROQ = "gsk_abcdefghijklmnopqrstuvwxyz012345"


@pytest.mark.parametrize("secret", [GOOGLE, OPENAI, GROQ])
def test_a_key_never_survives(secret: str):
    text = f"provider rejected the call: {secret} is not authorised"
    assert secret not in scrub(text)
    assert "redacted" in scrub(text)


def test_a_key_in_a_query_string_goes_but_the_url_stays_readable():
    """
    The URL is the useful half of the diagnostic. Only the value goes.
    """
    text = (
        "gemini: 400 from https://generativelanguage.googleapis.com/v1beta/"
        f"models/gemini-2.0-flash:generateContent?key={GOOGLE}"
    )
    out = scrub(text)
    assert GOOGLE not in out
    assert "key=<redacted>" in out
    # Still says which provider, which endpoint and which model.
    assert "generativelanguage.googleapis.com" in out
    assert "gemini-2.0-flash" in out


def test_a_bearer_token_goes_but_the_header_is_still_recognisable():
    text = "headers {'Authorization': 'Bearer ya29.a0AfB_abcdefghijklmnop'}"
    out = scrub(text)
    assert "ya29" not in out
    assert "Bearer <redacted>" in out


def test_the_diagnosis_is_left_intact():
    """
    Redaction that eats the reason would trade one unreadable error for
    another. These are the messages that actually tell somebody what to fix.
    """
    for text in (
        "groq: no key configured (GROQ_API_KEY is unset)",
        "gemini: 404 model gemini-2.0-flash is not found for API version v1beta",
        "gemini: 429 RESOURCE_EXHAUSTED quota exceeded for generate_content_free_tier",
        "ollama: nothing listening on OLLAMA_BASE_URL",
        "gemini: 403 SERVICE_DISABLED Generative Language API has not been used",
    ):
        assert scrub(text) == text, f"redaction ate a diagnostic: {text}"


def test_model_and_variable_names_are_not_mistaken_for_secrets():
    """The long-token rule must not fire on ordinary identifiers."""
    for word in (
        "gemini-2.0-flash",
        "llama-3.3-70b-versatile",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "n5_5_suppression_gate",
        "claude-sonnet-4-5",
    ):
        assert scrub(word) == word


def test_anything_long_enough_to_be_a_credential_goes_even_unlabelled():
    """
    The catch-all. A key shape nobody anticipated is still long, and a
    32-character run of key characters is not a word.
    """
    unknown = "Zk9" + "x" * 40
    assert unknown not in scrub(f"failed with {unknown}")


def test_empty_and_clean_text_pass_through():
    assert scrub("") == ""
    assert scrub("everything worked") == "everything worked"


def test_the_llm_layer_scrubs_on_the_way_out():
    """
    The wiring, not just the function: an exhausted provider chain must raise
    a message that has already been through the scrubber.
    """
    from src.integrations import llm

    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete(
            "prompt",
            task="a_task_with_no_mock_handler",
            chain=("mock",),
            allow_mock=False,
        )
    # Nothing to redact here, but the message must name what was tried rather
    # than being empty -- that emptiness was the original bug.
    assert "a_task_with_no_mock_handler" in str(caught.value)


def test_a_provider_skipped_for_a_missing_key_says_so():
    """
    A chain where every provider was merely unavailable used to raise with no
    reasons at all, which is how "(LLMUnavailable)" reached the screen.
    """
    from src.integrations import llm

    with pytest.raises(llm.LLMUnavailable) as caught:
        llm.complete("prompt", task="t", chain=("groq",))
    message = str(caught.value)
    assert "groq" in message
    assert "GROQ_API_KEY" in message
