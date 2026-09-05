"""
N7 reply monitoring and N8 follow-up sequencer tests (Build step 8).

The two behaviours that would do real damage if wrong:
  * an out-of-office autoreply must never be counted as a reply;
  * an `interested` reply must exit the sequence immediately, so nobody who
    said yes receives another scheduled touch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.integrations import email_reader, llm, suppression
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n6b_linkedin_outreach import enqueue, mark
from src.nodes.n7_reply_monitoring import (
    classify_offline,
    classify_reply,
    detect_out_of_office,
    log_manual_reply,
    n7_reply_monitoring,
    route_after_reply,
)
from src.nodes.n8_followup_sequencer import (
    is_touch_due,
    n8_followup_sequencer,
    route_after_sequencer,
    step_config,
)
from src.state import new_lead_state, utcnow

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("SUPPRESSION_BACKEND", "local")
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


def lead(**overrides):
    channel = overrides.pop("channel", "email")
    base = dict(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US",
        company_name="Lakeside Family Dentistry",
        contact_email="frontdesk@lakeside.com", contact_name="Dana Whitfield",
    )
    base.update(overrides)
    state = new_lead_state(**base)
    state["channel"] = channel
    state["signals"] = ["no online booking system"]
    state["suppression_status"] = "clear"
    state["sent_at"] = utcnow() - timedelta(days=3)
    state["last_touch_at"] = state["sent_at"]
    return state


def message(body: str, *, subject: str = "Re: your booking page", headers=None):
    return email_reader.IncomingMessage(
        from_address="frontdesk@lakeside.com",
        subject=subject,
        body=body,
        received_at=utcnow(),
        raw_headers=headers or {},
    )


# =========================================================================== #
# Out-of-office detection -- BEFORE classification
# =========================================================================== #

@pytest.mark.parametrize(
    "body",
    [
        "I am currently out of the office until 14 March with limited access to email.",
        "Thank you for your email. I am on annual leave and will be back on Monday.",
        "Automatic reply: I am away from my desk.",
        "Je suis absent du bureau, de retour le 14 mars.",
        "Ich bin im Urlaub und nicht im Büro.",
        "Estoy fuera de la oficina esta semana.",
    ],
)
def test_out_of_office_bodies_are_detected(body):
    is_ooo, reason = detect_out_of_office(body)
    assert is_ooo, body
    assert reason


def test_out_of_office_subject_prefix_is_detected():
    is_ooo, reason = detect_out_of_office("short text", subject="Out of Office: Re: hello")
    assert is_ooo
    assert "subject" in reason


def test_rfc3834_header_is_detected():
    msg = message("Anything at all.", headers={"auto-submitted": "auto-replied"})
    is_ooo, reason = detect_out_of_office(msg.body, message=msg)
    assert is_ooo
    assert "Auto-Submitted" in reason


def test_precedence_bulk_header_is_detected():
    msg = message("Anything.", headers={"precedence": "bulk"})
    assert detect_out_of_office(msg.body, message=msg)[0]


@pytest.mark.parametrize(
    "body",
    [
        "Thanks, that is timely. Can you do a 20 minute call Thursday?",
        "Not interested, please remove me.",
        "We already use a booking system, what makes yours different?",
    ],
)
def test_real_replies_are_not_flagged_as_autoreplies(body):
    assert not detect_out_of_office(body)[0]


def test_ooo_short_circuits_before_the_classifier(config, monkeypatch):
    """
    An autoreply reaching the classifier would often score as `interested`
    ("I will get back to you"), pulling the lead out of its cadence for nothing.
    """
    def explode(*args, **kwargs):
        raise AssertionError("an autoreply must never reach the classifier")

    monkeypatch.setattr("src.nodes.n7_reply_monitoring.classify_reply", explode)

    class Reader(email_reader.EmailReader):
        provider = "test"

        def fetch_replies(self, from_address, since, limit=10):
            return [message(
                "I am out of the office until 14 March. I will respond when I return."
            )]

    monkeypatch.setattr(email_reader, "get_reader", lambda **kwargs: Reader())
    monkeypatch.setattr(
        "src.nodes.n7_reply_monitoring.email_reader.get_reader", lambda **kwargs: Reader()
    )

    update = n7_reply_monitoring(lead(), tenant_config=config)
    assert update["reply_category"] == "out_of_office"


# =========================================================================== #
# Quoted-thread stripping
# =========================================================================== #

def test_quoted_thread_is_stripped():
    """
    Our own outgoing copy is quoted below the reply. Classifying it would find
    our own opt-out line in every reply.
    """
    raw = (
        "Not interested, thanks.\n\n"
        "On Mon, 2 Mar 2026, the sender wrote:\n"
        "> I noticed your booking page is phone-only.\n"
        '> Reply "unsubscribe" and I will not follow up.\n'
    )
    stripped = email_reader.strip_quoted_reply(raw)
    assert stripped == "Not interested, thanks."
    assert "unsubscribe" not in stripped


def test_stripping_leaves_an_unquoted_reply_alone():
    assert email_reader.strip_quoted_reply("Sure, Thursday works.") == "Sure, Thursday works."


# =========================================================================== #
# Classification
# =========================================================================== #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Yes, Thursday at 2 works.", "interested"),
        ("Can you send me pricing?", "interested"),
        ("Tell me more.", "interested"),
        ("Not interested, thanks.", "not_interested"),
        ("Please remove me from your list.", "not_interested"),
        ("We are all set, thanks.", "not_interested"),
        ("Too expensive for us right now.", "objection"),
        ("How did you get my email address?", "objection"),
    ],
)
def test_offline_classifier(text, expected):
    category, _, _ = classify_offline(text)
    assert category == expected


def test_a_refusal_outranks_an_enthusiasm_word():
    """
    "No thanks, but this looks interesting" must be not_interested. Getting
    this backwards keeps mailing someone who said no.
    """
    category, _, _ = classify_offline("No thanks, but this looks interesting.")
    assert category == "not_interested"


def test_an_unclear_reply_becomes_an_objection_not_a_no_reply(config):
    """A person who wrote back is a live conversation, not silence."""
    category, _ = classify_reply("Hmm.", lead())
    assert category == "objection"


def test_classification_failure_degrades_to_objection(config, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("all providers down")

    monkeypatch.setattr(llm, "complete_json", explode)
    category, reason = classify_reply("Anything", lead())
    assert category == "objection"
    assert "failed" in reason


# =========================================================================== #
# Auto-suppression at classification time
# =========================================================================== #

def test_a_not_interested_reply_suppresses_immediately(config, monkeypatch):
    class Reader(email_reader.EmailReader):
        provider = "test"

        def fetch_replies(self, from_address, since, limit=10):
            return [message("Not interested, please remove me from your list.")]

    monkeypatch.setattr(
        "src.nodes.n7_reply_monitoring.email_reader.get_reader", lambda **kwargs: Reader()
    )

    state = lead()
    update = n7_reply_monitoring(state, tenant_config=config)
    assert update["reply_category"] == "not_interested"
    assert update["suppression_status"] == "blocked_optout"
    assert suppression.for_tenant(EXAMPLE).is_suppressed(
        email=state["contact_email"]
    )[0]


def test_a_manual_linkedin_reply_takes_the_same_path(config):
    """A "stop contacting me" typed in from LinkedIn must suppress too."""
    state = lead(channel="linkedin", linkedin_url="https://linkedin.com/in/dana")
    update = log_manual_reply(state, "Please do not contact me again.")
    assert update["reply_category"] == "not_interested"
    assert suppression.for_tenant(EXAMPLE).is_suppressed(
        linkedin_url="https://linkedin.com/in/dana"
    )[0]


def test_a_manual_out_of_office_entry_is_not_a_reply(config):
    state = lead()
    update = log_manual_reply(state, "I am on annual leave until Monday.")
    assert update["reply_category"] == "out_of_office"
    assert not suppression.for_tenant(EXAMPLE).is_suppressed(
        email=state["contact_email"]
    )[0]


def test_no_messages_means_no_reply(config, monkeypatch):
    monkeypatch.setattr(
        "src.nodes.n7_reply_monitoring.email_reader.get_reader",
        lambda **kwargs: email_reader.NullReader(),
    )
    assert n7_reply_monitoring(lead(), tenant_config=config)["reply_category"] == "no_reply"


def test_a_lead_with_nothing_sent_yet_is_no_reply(config):
    state = lead()
    state["sent_at"] = None
    assert n7_reply_monitoring(state, tenant_config=config)["reply_category"] == "no_reply"


def test_an_already_recorded_reply_is_not_reclassified(config, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not re-fetch a reply we already have")

    monkeypatch.setattr("src.nodes.n7_reply_monitoring.email_reader.get_reader", explode)
    state = lead()
    state["reply_text"] = "Yes please."
    state["reply_category"] = "interested"
    assert set(n7_reply_monitoring(state, tenant_config=config)) <= {"last_updated"}


@pytest.mark.parametrize(
    "category,expected",
    [
        ("interested", "manual_handoff"),
        ("not_interested", "sequencer"),
        ("objection", "sequencer"),
        ("out_of_office", "sequencer"),
        ("no_reply", "sequencer"),
    ],
)
def test_route_after_reply(category, expected):
    state = lead()
    state["reply_category"] = category
    assert route_after_reply(state) == expected


# =========================================================================== #
# N8 -- the interested-reply rule
# =========================================================================== #

def test_interested_exits_the_sequence_immediately(config):
    """The single most damaging mistake this system could make."""
    state = lead()
    state["sequence_step"] = 1
    state["reply_category"] = "interested"
    state["reply_text"] = "Yes, Thursday works."

    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["next_touch_due"] is None
    assert update["archived"] is True
    assert update["archive_reason"] == "replied_interested"
    assert update["sequence_step"] == 1, "an interested reply must not advance the cadence"
    assert "MANUAL" in update["next_action"]
    assert "same-day" in update["next_action"]


def test_interested_at_the_very_first_touch_still_exits(config):
    state = lead()
    state["sequence_step"] = 0
    state["reply_category"] = "interested"
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["archived"] is True
    assert update["next_touch_due"] is None


def test_not_interested_closes_the_sequence(config):
    state = lead()
    state["sequence_step"] = 1
    state["reply_category"] = "not_interested"
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["archived"] is True
    assert update["next_touch_due"] is None


def test_an_objection_stops_the_cadence_and_asks_for_a_human(config):
    state = lead()
    state["sequence_step"] = 1
    state["reply_category"] = "objection"
    state["reply_text"] = "Too expensive."
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["needs_manual_review"] is True
    assert update["next_touch_due"] is None


def test_out_of_office_defers_without_burning_a_touch(config):
    state = lead()
    state["sequence_step"] = 1
    state["reply_category"] = "out_of_office"
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["sequence_step"] == 1, "an OOO must not consume a cadence touch"
    assert update["next_touch_due"] > utcnow()
    assert update["reply_category"] == "no_reply", "the OOO must be cleared for next time"
    assert not update.get("archived")


# =========================================================================== #
# N8 -- cadence advancement
# =========================================================================== #

def test_first_touch_advances_to_the_second(config):
    state = lead()
    state["sequence_step"] = 0
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["sequence_step"] == 1
    assert update["next_touch_due"] is not None
    assert "touch 2" in update["next_action"]


def test_the_tenant_override_gap_is_used(config):
    """example_tenant overrides step 2's wait_days from the default 4 to 3."""
    state = lead()
    state["sequence_step"] = 0
    base = state["last_touch_at"]
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["next_touch_due"] == base + timedelta(days=3)


def test_advancing_resets_the_per_touch_state(config):
    """The next touch must be approved again; it does not inherit approval."""
    state = lead()
    state["sequence_step"] = 0
    state["approval_status"] = "approved"
    state["send_status"] = "sent"
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["approval_status"] == "pending"
    assert update["send_status"] == "not_sent"


def test_the_cadence_ends_and_archives(config):
    state = lead()
    state["sequence_step"] = 3          # the 3-touch email cadence is done
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["archived"] is True
    assert update["archive_reason"] == "cadence_exhausted"
    assert update["next_touch_due"] is None


def test_the_region_cap_beats_a_longer_cadence(config):
    """EU caps at 2 touches even though the email cadence has 3 steps."""
    state = lead(region="EU", niche_id="local_salon", contact_email="contact@atelier.fr")
    state["sequence_step"] = 2
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["archived"] is True
    assert "region cap of 2" in update["next_action"]


def test_us_uses_the_full_cadence(config):
    state = lead(region="US")
    state["sequence_step"] = 1
    update = n8_followup_sequencer(state, tenant_config=config)
    assert not update.get("archived")
    assert update["sequence_step"] == 2


def test_every_region_and_channel_has_a_reachable_ceiling(config):
    for region in config.regions:
        for channel in ("email", "linkedin", "both"):
            assert config.max_touches(channel, region) >= 1


# =========================================================================== #
# N8 -- LinkedIn acceptance gating
# =========================================================================== #

def test_the_dm_step_waits_for_the_connection_to_be_accepted(config):
    # seq=0 on entry means touch 1 (the connection request) has just been sent;
    # see the sequence_step convention in n8_followup_sequencer's docstring.
    state = lead(channel="linkedin", linkedin_url="https://linkedin.com/in/dana")
    state["sequence_step"] = 0
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["sequence_step"] == 0, "the touch must not be consumed while waiting"
    assert "waiting for the LinkedIn connection" in update["next_action"]


def test_marking_connected_releases_the_dm_step(config):
    state = lead(channel="linkedin", linkedin_url="https://linkedin.com/in/dana")
    state["sequence_step"] = 0
    enqueue(EXAMPLE, {
        "lead_id": state["lead_id"], "sequence_step": 1, "company_name": "Lakeside",
        "linkedin_url": "https://linkedin.com/in/dana", "connection_note": "hi",
    })
    mark(EXAMPLE, state["lead_id"], "connected")

    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["sequence_step"] == 1
    assert update["next_touch_due"] is not None


def test_an_unaccepted_connection_is_abandoned_eventually(config):
    """cadences.yaml sets abandon_if_unmet_after_days: 21 on the DM step."""
    state = lead(channel="linkedin", linkedin_url="https://linkedin.com/in/dana")
    state["sequence_step"] = 0
    state["last_touch_at"] = utcnow() - timedelta(days=30)
    update = n8_followup_sequencer(state, tenant_config=config)
    assert update["archived"] is True
    assert update["archive_reason"] == "connection_not_accepted"


# =========================================================================== #
# Cadence config plumbing
# =========================================================================== #

def test_step_config_reads_the_merged_cadence(config):
    step = step_config(config, "email", 2)
    assert step["name"] == "value_nudge"
    assert step["wait_days"] == 3        # tenant override
    assert step["intent"]                # default text survived the merge


def test_both_channel_uses_the_interleaved_cadence(config):
    assert step_config(config, "both", 2)["channel"] == "linkedin"
    assert step_config(config, "both", 1)["channel"] == "email"


def test_is_touch_due():
    state = lead()
    state["next_touch_due"] = utcnow() - timedelta(hours=1)
    assert is_touch_due(state)
    state["next_touch_due"] = utcnow() + timedelta(days=1)
    assert not is_touch_due(state)
    state["next_touch_due"] = None
    assert not is_touch_due(state)


@pytest.mark.parametrize(
    "archived,due,expected",
    [
        (True, None, "crm"),
        (False, None, "crm"),
        (False, datetime(2026, 5, 1, tzinfo=timezone.utc), "personalization"),
    ],
)
def test_route_after_sequencer(archived, due, expected):
    state = lead()
    state["archived"] = archived
    state["next_touch_due"] = due
    assert route_after_sequencer(state) == expected
