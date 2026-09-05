"""
N3 qualification tests (Build step 4).

The LLM is exercised through the deterministic `mock` provider, which applies
the same rubric the prompt describes -- so these tests cover the real prompt
construction, the real JSON parsing and the real threshold routing without a
network call or an API key.
"""

from __future__ import annotations

import json

import pytest

from src.integrations import llm
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n3_qualification import (
    _coerce_score,
    build_prompt,
    n3_qualification,
    route_after_qualification,
    score_offline,
)
from src.state import new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _force_mock_provider(monkeypatch):
    """No API keys in the test environment -> the mock provider serves calls."""
    for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


def lead(**overrides):
    base = dict(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile Dental", website="https://bright.co.uk",
        contact_email="hello@bright.co.uk", contact_name="Priya Raman",
    )
    signals = overrides.pop("signals", None)
    base.update(overrides)
    state = new_lead_state(**base)
    if signals is not None:
        state["signals"] = signals
    return state


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

def test_prompt_includes_the_icp_and_the_evidence(config):
    state = lead(signals=["no online booking system", "34 unanswered reviews"])
    prompt = build_prompt(state, config.niche("local_dental"))
    assert "Bright Smile Dental" in prompt
    assert "no online booking system" in prompt
    assert "34 unanswered reviews" in prompt
    assert "Owner-operated or small-group dental practices" in prompt
    # Disqualifiers must reach the model, not just the good signals.
    assert "national chain" in prompt


def test_prompt_marks_missing_evidence_explicitly(config):
    prompt = build_prompt(lead(signals=[]), config.niche("local_dental"))
    assert "no signals found during enrichment" in prompt


# --------------------------------------------------------------------------- #
# The offline rubric
# --------------------------------------------------------------------------- #

def test_no_signals_scores_low(config):
    score, reason = score_offline(lead(signals=[]), config.niche("local_dental"))
    assert score <= 35
    assert "no signals" in reason.lower()


def test_matching_icp_signals_scores_high(config):
    state = lead(signals=[
        "no online booking system",
        "unanswered Google reviews",
        "hiring front-desk or treatment coordinator staff",
    ])
    score, reason = score_offline(state, config.niche("local_dental"))
    assert score >= 60
    assert "ICP signal" in reason


def test_a_disqualifier_caps_the_score(config):
    state = lead(signals=["part of a national chain with 20+ locations"])
    score, reason = score_offline(state, config.niche("local_dental"))
    assert score <= 19
    assert "isqualif" in reason


def test_site_disqualifier_marker_caps_the_score(config):
    state = lead(signals=["DISQUALIFIER found on site: already running a chatbot"])
    score, _ = score_offline(state, config.niche("local_dental"))
    assert score <= 19


def test_generic_signals_score_in_the_middle(config):
    state = lead(signals=["public phone number 0161 555 0100"])
    score, _ = score_offline(state, config.niche("local_dental"))
    assert 20 <= score < 60


# --------------------------------------------------------------------------- #
# Score coercion -- models return every shape imaginable
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [(85, 85), ("85", 85), (85.4, 85), ("85/100", 85), ("score: 72", 72),
     (150, 100), (-10, 0), ("100", 100)],
)
def test_coerce_score(raw, expected):
    assert _coerce_score(raw) == expected


@pytest.mark.parametrize("raw", [True, "no number here", None])
def test_coerce_score_rejects_nonsense(raw):
    with pytest.raises((ValueError, TypeError)):
        _coerce_score(raw)


# --------------------------------------------------------------------------- #
# The node
# --------------------------------------------------------------------------- #

def test_node_scores_and_explains(config):
    state = lead(signals=["no online booking system", "unanswered Google reviews"])
    update = n3_qualification(state, tenant_config=config)
    assert 0 <= update["fit_score"] <= 100
    assert update["fit_reason"]
    assert not update.get("archived")


def test_node_archives_below_threshold(config):
    state = lead(signals=[])
    update = n3_qualification(state, tenant_config=config)
    assert update["fit_score"] < config.fit_score_threshold
    assert update["archived"] is True
    assert update["archive_reason"] == "below_threshold"


def test_node_flags_manual_review_when_every_provider_fails(config, monkeypatch):
    """Section 4, N3: a second failure routes the lead out, it does not block."""
    def explode(*args, **kwargs):
        raise llm.LLMUnavailable("all providers down")

    monkeypatch.setattr(llm, "complete_json", explode)
    update = n3_qualification(lead(signals=["x"]), tenant_config=config)
    assert update["needs_manual_review"] is True
    assert "n3_qualification" in update["manual_review_reason"]
    assert update["errors"][-1]["node"] == "n3_qualification"


def test_node_survives_a_garbage_model_reply(config, monkeypatch):
    """Unparseable JSON must not crash the batch."""
    monkeypatch.setattr(
        llm, "complete",
        lambda *a, **k: llm.LLMResponse(text="I think it's a good lead!", provider="mock"),
    )
    update = n3_qualification(lead(signals=["x"]), tenant_config=config)
    assert update["needs_manual_review"] is True


def test_node_accepts_a_fenced_json_reply(config, monkeypatch):
    """Models wrap JSON in code fences no matter what the prompt says."""
    payload = json.dumps({"fit_score": 77, "fit_reason": "Strong booking gap."})
    monkeypatch.setattr(
        llm, "complete",
        lambda *a, **k: llm.LLMResponse(
            text=f"Here you go:\n```json\n{payload}\n```", provider="mock"
        ),
    )
    update = n3_qualification(lead(signals=["x"]), tenant_config=config)
    assert update["fit_score"] == 77


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

def test_route_sends_a_qualified_lead_to_channel_selection(config):
    state = lead()
    state["fit_score"] = 80
    assert route_after_qualification(state) == "channel_selection"


def test_route_archives_below_threshold(config):
    state = lead()
    state["fit_score"] = 10
    assert route_after_qualification(state) == "archive"


def test_route_sends_a_failed_lead_to_manual_review(config):
    state = lead()
    state["fit_score"] = 90
    state["needs_manual_review"] = True
    assert route_after_qualification(state) == "manual_review"


def test_route_archives_an_unreachable_lead(config):
    state = lead()
    state["fit_score"] = 90
    state["unreachable"] = True
    assert route_after_qualification(state) == "archive"


# --------------------------------------------------------------------------- #
# Provider chain
# --------------------------------------------------------------------------- #

def test_mock_is_always_last_in_the_chain():
    chain = llm.resolve_provider_chain("groq")
    assert chain[0] == "groq"
    assert chain[-1] == "mock"


def test_preferred_provider_leads_the_chain():
    assert llm.resolve_provider_chain("ollama")[0] == "ollama"


def test_extract_json_handles_fences_and_prose():
    assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.extract_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}
    with pytest.raises(ValueError):
        llm.extract_json("no json at all")
