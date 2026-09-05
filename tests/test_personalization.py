"""
N4 personalization and N5 approval tests (Build step 5).

The three functional requirements under test:
  * a draft must demonstrably reference a specific signal, or the lead is
    flagged for manual drafting rather than sent generic copy;
  * a LinkedIn connection note is never over 300 characters;
  * a non-English region localises through translation.py.
"""

from __future__ import annotations

import json

import pytest

from src.integrations import llm, translation
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n4_personalization import (
    _forbidden_hits,
    draft_offline,
    n4_personalization,
    shorten_note,
    verify_references_signal,
)
from src.nodes.n5_human_approval import (
    apply_decision,
    build_review_payload,
    n5_human_approval,
    route_after_approval,
)
from src.state import MAX_LINKEDIN_CONNECTION_NOTE_CHARS, new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "DEEPL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


def lead(**overrides):
    signals = overrides.pop("signals", ["no online booking - phone number only on the contact page"])
    channel = overrides.pop("channel", "email")
    base = dict(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile Dental", website="https://bright.co.uk",
        contact_email="hello@bright.co.uk", contact_name="Priya Raman",
        dry_run=True,
    )
    base.update(overrides)
    state = new_lead_state(**base)
    state["signals"] = list(signals)
    state["channel"] = channel
    return state


# --------------------------------------------------------------------------- #
# The self-check
# --------------------------------------------------------------------------- #

def test_self_check_accepts_a_draft_that_cites_the_signal():
    signals = ["no online booking - phone number only on the contact page"]
    draft = "I noticed your contact page only lists a phone number, with no online booking."
    ok, matched = verify_references_signal(draft, signals)
    assert ok
    assert matched == signals[0]


def test_self_check_rejects_a_generic_draft():
    signals = ["no online booking - phone number only on the contact page"]
    draft = "I came across your practice and thought I would introduce myself."
    ok, matched = verify_references_signal(draft, signals)
    assert not ok
    assert matched == ""


def test_self_check_does_not_trust_the_models_own_claim():
    """
    A model will happily claim it referenced a signal it ignored. The claim is
    checked by the same evidence standard as anything else.
    """
    signals = ["three open Customer Success roles posted in the last 30 days"]
    draft = "Hope you are well. Would you be open to a chat next week?"
    ok, _ = verify_references_signal(draft, signals, claimed=signals[0])
    assert not ok


def test_self_check_fails_when_there_are_no_signals():
    ok, _ = verify_references_signal("Anything at all here.", [])
    assert not ok


def test_forbidden_phrase_detection(config):
    forbidden = config.personalization["forbidden_phrases"]
    assert _forbidden_hits("I hope this email finds you well, quick question", forbidden)
    assert not _forbidden_hits("I noticed your booking page is phone-only.", forbidden)


# --------------------------------------------------------------------------- #
# Offline drafting
# --------------------------------------------------------------------------- #

def test_offline_draft_cites_the_signal():
    state = lead()
    draft = draft_offline(state, "email", "")
    ok, _ = verify_references_signal(draft["body"], state["signals"])
    assert ok


def test_offline_draft_with_no_signals_is_generic_by_design():
    """The fixture's Grantly lead exists to prove the self-check catches this."""
    state = lead(signals=[])
    draft = draft_offline(state, "email", "")
    ok, _ = verify_references_signal(draft["body"], state["signals"])
    assert not ok


# --------------------------------------------------------------------------- #
# The 300-character LinkedIn limit
# --------------------------------------------------------------------------- #

def test_node_never_emits_an_over_length_connection_note(config):
    state = lead(
        channel="linkedin",
        linkedin_url="https://linkedin.com/company/bright",
        signals=[
            "the online booking widget on the appointments page returns a 404 error "
            "on mobile Safari and has done since at least January according to the "
            "wayback machine snapshots of the practice website"
        ],
    )
    update = n4_personalization(state, tenant_config=config)
    note = update["draft_message"]["linkedin"]["connection_note"]
    assert len(note) <= MAX_LINKEDIN_CONNECTION_NOTE_CHARS


def test_shorten_note_leaves_a_short_note_alone(config):
    note = "Hi Priya - noticed your booking page is phone-only. Happy to connect."
    assert shorten_note(note, lead(), config) == note


def test_over_length_note_after_shortening_fails_the_self_check(config, monkeypatch):
    """
    A note that cannot be brought under the limit must flag manual drafting,
    NOT be truncated mid-sentence.
    """
    long_note = "x" * 400

    def stubborn(*args, **kwargs):
        return (
            {"connection_note": long_note, "followup_dm": "hi"},
            json.dumps({"connection_note": long_note}),
        )

    monkeypatch.setattr(
        "src.nodes.n4_personalization.draft_linkedin",
        lambda *a, **k: ({"connection_note": long_note, "followup_dm": ""}, ""),
    )
    monkeypatch.setattr(
        "src.nodes.n4_personalization.shorten_note", lambda note, *a, **k: note
    )

    state = lead(channel="linkedin", linkedin_url="https://linkedin.com/company/x")
    update = n4_personalization(state, tenant_config=config)
    assert update["needs_manual_review"] is True
    assert "300" in update["manual_review_reason"]


# --------------------------------------------------------------------------- #
# Manual-drafting fallback
# --------------------------------------------------------------------------- #

def test_no_signals_flags_manual_drafting_instead_of_sending_generic_copy(config):
    state = lead(signals=[])
    update = n4_personalization(state, tenant_config=config)
    assert update["needs_manual_review"] is True
    assert "self-check failed" in update["manual_review_reason"]
    assert "draft_message" not in update
    assert update["next_action"].startswith("MANUAL")


def test_self_check_runs_the_configured_number_of_attempts(config, monkeypatch):
    attempts = {"count": 0}

    def generic(state, cfg, step, *args, **kwargs):
        attempts["count"] += 1
        return {"subject": "hello", "body": "I came across your company."}, ""

    monkeypatch.setattr("src.nodes.n4_personalization.draft_email", generic)
    n4_personalization(lead(), tenant_config=config)
    assert attempts["count"] == config.personalization["max_self_check_attempts"]


# --------------------------------------------------------------------------- #
# Opt-out line and localisation
# --------------------------------------------------------------------------- #

def test_optout_line_is_appended_to_every_email(config):
    update = n4_personalization(lead(), tenant_config=config)
    body = update["draft_message"]["email"]["body"]
    assert "unsubscribe" in body.lower()


def test_english_regions_are_not_translated(config):
    update = n4_personalization(lead(region="UK"), tenant_config=config)
    assert update["draft_message"]["translated"] is False
    assert update["draft_message"]["language"] == "en"


def test_french_eu_lead_goes_through_the_translation_path(config, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_translate_fields(fields, language, provider=None):
        calls.append((tuple(fields), language))
        return ({k: f"[FR] {v}" for k, v in fields.items()}, True, "deepl")

    monkeypatch.setattr(translation, "translate_fields", fake_translate_fields)
    state = lead(
        niche_id="local_salon", region="EU", language="fr",
        company_name="Atelier Coiffure Saint-Germain",
        contact_email="contact@atelier.fr",
        signals=["reservation page is a mailto link with no calendar or availability"],
    )
    update = n4_personalization(state, tenant_config=config)
    assert calls and calls[0][1] == "fr"
    assert update["draft_message"]["translated"] is True
    assert update["draft_message"]["email"]["body"].startswith("[FR]")


def test_failed_translation_keeps_the_english_text(config, monkeypatch):
    monkeypatch.setattr(
        translation,
        "translate_fields",
        lambda fields, language, provider=None: (fields, False, "none"),
    )
    state = lead(niche_id="local_salon", region="EU", language="fr",
                 contact_email="contact@atelier.fr")
    update = n4_personalization(state, tenant_config=config)
    assert update["draft_message"]["translated"] is False
    assert update["draft_message"]["email"]["body"]      # still sendable


def test_translation_never_raises_on_provider_failure(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("deepl down")

    monkeypatch.setattr(translation, "_translate_llm", explode)
    result = translation.translate("Hello there", "fr")
    assert result.text == "Hello there"
    assert result.translated is False


def test_english_target_is_a_no_op():
    result = translation.translate("Hello there", "en")
    assert result.translated is False
    assert result.provider == "none"


# --------------------------------------------------------------------------- #
# Channel coverage
# --------------------------------------------------------------------------- #

def test_both_channel_drafts_email_and_linkedin(config):
    state = lead(channel="both", linkedin_url="https://linkedin.com/company/bright")
    draft = n4_personalization(state, tenant_config=config)["draft_message"]
    assert draft["email"]["subject"] and draft["email"]["body"]
    assert draft["linkedin"]["connection_note"]


def test_email_channel_drafts_no_linkedin(config):
    draft = n4_personalization(lead(channel="email"), tenant_config=config)["draft_message"]
    assert "email" in draft
    assert "linkedin" not in draft


def test_subsequent_touches_use_the_next_cadence_step(config):
    state = lead()
    state["sequence_step"] = 2
    update = n4_personalization(state, tenant_config=config)
    assert "touch 3" in update["next_action"]


# --------------------------------------------------------------------------- #
# N5 approval
# --------------------------------------------------------------------------- #

def test_review_payload_contains_what_an_operator_needs(config):
    state = lead(channel="both", linkedin_url="https://linkedin.com/company/x")
    state.update(n4_personalization(state, tenant_config=config))
    state["fit_score"] = 82
    state["fit_reason"] = "Strong booking gap."
    payload = build_review_payload(state)
    for key in ("company_name", "channel", "fit_reason", "email", "linkedin", "signals"):
        assert key in payload
    assert payload["fit_score"] == 82


@pytest.mark.parametrize("decision", ["a", "approve", {"action": "a"}, {"action": "approve"}])
def test_approve_decisions(decision):
    update = apply_decision(lead(), decision)
    assert update["approval_status"] == "approved"
    assert not update.get("archived")


@pytest.mark.parametrize("decision", ["r", "reject", {"action": "r"}])
def test_reject_archives_the_lead(decision):
    update = apply_decision(lead(), decision)
    assert update["approval_status"] == "rejected"
    assert update["archived"] is True
    assert update["archive_reason"] == "rejected"


def test_edit_replaces_the_draft_text():
    state = lead()
    state["draft_message"] = {"email": {"subject": "old", "body": "old body"}}
    update = apply_decision(state, {
        "action": "edit", "email": {"subject": "new", "body": "new body"},
    })
    assert update["approval_status"] == "edited"
    assert update["draft_message"]["email"]["subject"] == "new"
    assert update["draft_message"]["edited_by_human"] is True


def test_edit_preserves_untouched_fields():
    state = lead()
    state["draft_message"] = {
        "email": {"subject": "keep", "body": "old"},
        "signal_referenced": "no online booking",
    }
    update = apply_decision(state, {"action": "edit", "email": {"body": "new"}})
    assert update["draft_message"]["email"]["subject"] == "keep"
    assert update["draft_message"]["signal_referenced"] == "no online booking"


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError, match="unknown approval action"):
        apply_decision(lead(), "maybe")


def test_dry_run_auto_approves_without_interrupting(config):
    """A dry run must be able to exercise the whole graph unattended."""
    state = lead()
    state["draft_message"] = {"email": {"subject": "s", "body": "b"}}
    update = n5_human_approval(state, tenant_config=config)
    assert update["approval_status"] == "approved"
    assert "dry run" in update["next_action"]


def test_an_already_decided_lead_does_not_interrupt_again(config):
    state = lead(dry_run=False)
    state["approval_status"] = "approved"
    # @node always refreshes last_updated; nothing else should change.
    assert set(n5_human_approval(state, tenant_config=config)) == {"last_updated"}


# --------------------------------------------------------------------------- #
# The interrupt / resume mechanism itself
# --------------------------------------------------------------------------- #

def _approval_graph(db_path):
    """A one-node graph around N5, checkpointed to `db_path`."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    from src.state import LeadState as LeadStateType

    builder = StateGraph(LeadStateType)
    builder.add_node("approve", n5_human_approval)
    builder.add_edge(START, "approve")
    builder.add_edge("approve", END)
    saver_ctx = SqliteSaver.from_conn_string(str(db_path))
    saver = saver_ctx.__enter__()
    return builder.compile(checkpointer=saver), saver_ctx


def test_a_live_run_interrupts_and_resumes(tmp_path, config):
    """
    The core of N5: the thread pauses, the state is checkpointed, and resuming
    with Command(resume=...) applies the decision.
    """
    from langgraph.types import Command

    db = tmp_path / "approval.sqlite"
    graph, ctx = _approval_graph(db)
    try:
        state = lead(dry_run=False)
        state["draft_message"] = {"email": {"subject": "s", "body": "b"}}
        cfg = {"configurable": {"thread_id": state["lead_id"]}}

        result = graph.invoke(state, config=cfg)
        assert "__interrupt__" in result, "the thread should have paused for review"
        payload = result["__interrupt__"][0].value
        assert payload["company_name"] == "Bright Smile Dental"
        assert payload["email"]["subject"] == "s"

        resumed = graph.invoke(Command(resume={"action": "approve"}), config=cfg)
        assert resumed["approval_status"] == "approved"
    finally:
        ctx.__exit__(None, None, None)


def test_the_approval_queue_survives_a_process_restart(tmp_path, config):
    """
    Section 4, N5: "must survive the process exiting and restarting". The
    second graph object below shares nothing with the first except the file.
    """
    from langgraph.types import Command

    db = tmp_path / "approval.sqlite"
    state = lead(dry_run=False)
    state["draft_message"] = {"email": {"subject": "s", "body": "b"}}
    cfg = {"configurable": {"thread_id": state["lead_id"]}}

    graph, ctx = _approval_graph(db)
    try:
        assert "__interrupt__" in graph.invoke(state, config=cfg)
    finally:
        ctx.__exit__(None, None, None)

    # ... process exits here; a completely new saver + graph picks it up.
    graph2, ctx2 = _approval_graph(db)
    try:
        snapshot = graph2.get_state(cfg)
        assert snapshot.interrupts, "the interrupt must be recoverable from disk"
        assert snapshot.values["company_name"] == "Bright Smile Dental"

        resumed = graph2.invoke(
            Command(resume={"action": "edit", "email": {"body": "rewritten by hand"}}),
            config=cfg,
        )
        assert resumed["approval_status"] == "edited"
        assert resumed["draft_message"]["email"]["body"] == "rewritten by hand"
    finally:
        ctx2.__exit__(None, None, None)


def test_node_decorator_does_not_swallow_the_interrupt(tmp_path, config):
    """
    Regression guard: @node has a deliberate catch-all for Section 8, and
    GraphInterrupt travels through it. If it is ever swallowed, every approval
    silently becomes a needs_manual_review flag instead of a pause.
    """
    db = tmp_path / "approval.sqlite"
    graph, ctx = _approval_graph(db)
    try:
        state = lead(dry_run=False)
        state["draft_message"] = {"email": {"subject": "s", "body": "b"}}
        result = graph.invoke(state, config={"configurable": {"thread_id": "x"}})
        assert "__interrupt__" in result
        assert not result.get("needs_manual_review")
    finally:
        ctx.__exit__(None, None, None)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("approved", "suppression_gate"),
        ("edited", "suppression_gate"),
        ("rejected", "archive"),
        ("pending", "archive"),
    ],
)
def test_route_after_approval(status, expected):
    state = lead()
    state["approval_status"] = status
    assert route_after_approval(state) == expected


def test_route_after_approval_sends_failed_drafts_to_manual_review():
    state = lead()
    state["approval_status"] = "approved"
    state["needs_manual_review"] = True
    assert route_after_approval(state) == "manual_review"
