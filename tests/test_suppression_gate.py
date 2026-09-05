"""
N5.5 suppression and compliance gate tests (Build step 6).

Covers:
  * exact-match blocking on email and on LinkedIn URL
  * region compliance failures (missing opt-out line, and so on)
  * auto-suppression on a not_interested or opt-out reply
  * MULTI-TENANCY: tenant A's suppression list is never consulted for a
    tenant B lead, and the reverse (Section 7, non-negotiable)
"""

from __future__ import annotations

import copy

import pytest
import yaml

from src.integrations import suppression
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n5_5_suppression_gate import (
    auto_suppress_from_reply,
    check_compliance,
    n5_5_suppression_gate,
    route_after_suppression,
    should_auto_suppress,
)
from src.settings import ROOT, suppression_path
from src.state import new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("SUPPRESSION_BACKEND", "local")   # no CRM mirror in tests
    monkeypatch.setenv("BLOCKED_DOMAINS", "example.com,test.com")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


# The sender identity comes from the tenant config, never from a name written
# into a test. The rule under test is "the sender must be identifiable in the
# body" -- pinning that to one person's name tested the fixture, not the rule,
# and broke the moment the shipped config stopped naming anybody.
_IDENTITY = load_tenant_config(EXAMPLE).sending_identity
SENDER_NAME = _IDENTITY["from_name"]
SENDER_COMPANY = _IDENTITY["company_name"]
SENDER_ADDRESS = _IDENTITY["physical_address"]

#: The signature block a real draft carries, built from that identity.
SIGNATURE = f"{SENDER_NAME}, {SENDER_COMPANY}"


def compliant_lead(**overrides):
    """
    A lead whose draft satisfies every US rule: opt-out line, sender
    identification, and the physical postal address CAN-SPAM requires.
    """
    signals = overrides.pop("signals", ["no online booking system"])
    channel = overrides.pop("channel", "email")
    body = overrides.pop("body", None)
    base = dict(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US",
        company_name="Lakeside Family Dentistry",
        website="https://lakeside.com",
        contact_email="frontdesk@lakeside.com",
        contact_name="Dana Whitfield",
    )
    base.update(overrides)
    state = new_lead_state(**base)
    state["signals"] = signals
    state["channel"] = channel
    state["approval_status"] = "approved"
    state["draft_message"] = {
        "email": {
            "subject": "your booking page",
            "body": body if body is not None else (
                "Hi Dana,\n\nI noticed your online booking system is missing.\n\n"
                f"{SIGNATURE}\n"
                f"{SENDER_ADDRESS}\n"
                'If this is not relevant, reply "unsubscribe" and I will not follow up.'
            ),
        },
        "signal_referenced": "no online booking system",
    }
    return state


# =========================================================================== #
# MULTI-TENANCY -- Section 7, non-negotiable
# =========================================================================== #

def test_tenant_a_suppression_is_never_consulted_for_tenant_b():
    """The core Section 7 guarantee, in the A -> B direction."""
    email = "shared.contact@acme.com"
    suppression.for_tenant("tenant_a").add(email=email, reason="opted out of A")

    blocked_a, reason_a = suppression.for_tenant("tenant_a").is_suppressed(email=email)
    assert blocked_a and "opted out of A" in reason_a

    blocked_b, reason_b = suppression.for_tenant("tenant_b").is_suppressed(email=email)
    assert not blocked_b, "tenant B must not see tenant A's suppression entry"
    assert reason_b == ""


def test_tenant_b_suppression_is_never_consulted_for_tenant_a():
    """And in the B -> A direction, because asymmetric bugs are real."""
    email = "shared.contact@acme.com"
    suppression.for_tenant("tenant_b").add(email=email, reason="opted out of B")

    assert suppression.for_tenant("tenant_b").is_suppressed(email=email)[0]
    assert not suppression.for_tenant("tenant_a").is_suppressed(email=email)[0]


def test_linkedin_suppression_is_also_tenant_scoped():
    url = "https://www.linkedin.com/in/shared-person"
    suppression.for_tenant("tenant_a").add(linkedin_url=url, reason="opted out of A")
    assert suppression.for_tenant("tenant_a").is_suppressed(linkedin_url=url)[0]
    assert not suppression.for_tenant("tenant_b").is_suppressed(linkedin_url=url)[0]


def test_each_tenant_has_a_distinct_storage_file():
    assert suppression_path("tenant_a") != suppression_path("tenant_b")
    assert "tenant_a" in suppression_path("tenant_a").name
    assert "tenant_b" in suppression_path("tenant_b").name


def test_the_gate_itself_is_tenant_scoped(config):
    """
    End to end through the node: the same contact, suppressed for tenant A,
    must still be clear for the example tenant.
    """
    state = compliant_lead()
    suppression.for_tenant("tenant_a").add(
        email=state["contact_email"], reason="opted out of A"
    )
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "clear"

    suppression.for_tenant(EXAMPLE).add(
        email=state["contact_email"], reason="opted out of the example tenant"
    )
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"


def test_a_mismatched_tenant_id_in_the_file_is_refused():
    """A tampered or mis-copied file must not apply another tenant's list."""
    import json

    store = suppression.for_tenant("tenant_a")
    store.add(email="x@acme.com", reason="test")
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["tenant_id"] = "tenant_b"
    store.path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="declares tenant_id"):
        suppression.for_tenant("tenant_a").is_suppressed(email="x@acme.com")


def test_tenant_id_path_traversal_is_rejected():
    for bad in ("../other", "a/b", "..", "tenant a"):
        with pytest.raises(ValueError):
            suppression_path(bad)


# =========================================================================== #
# Exact-match blocking
# =========================================================================== #

def test_email_match_blocks(config):
    state = compliant_lead()
    suppression.for_tenant(EXAMPLE).add(
        email="frontdesk@lakeside.com", reason="manual opt-out request"
    )
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"
    assert update["archived"] is True
    assert update["archive_reason"] == "suppressed"


def test_email_match_is_case_insensitive(config):
    suppression.for_tenant(EXAMPLE).add(email="FrontDesk@Lakeside.COM", reason="x")
    state = compliant_lead(contact_email="frontdesk@lakeside.com")
    assert n5_5_suppression_gate(state, tenant_config=config)["suppression_status"] == (
        "blocked_optout"
    )


@pytest.mark.parametrize(
    "stored,checked",
    [
        ("https://www.linkedin.com/in/kieran/", "https://linkedin.com/in/kieran"),
        ("linkedin.com/in/kieran", "https://www.linkedin.com/in/kieran/"),
        ("https://uk.linkedin.com/in/kieran", "https://www.linkedin.com/in/kieran"),
        ("https://www.linkedin.com/in/kieran?utm=x", "https://linkedin.com/in/kieran"),
    ],
)
def test_linkedin_url_normalisation(stored, checked):
    """A list that misses one URL spelling is worse than no list."""
    store = suppression.for_tenant("tenant_norm")
    store.add(linkedin_url=stored, reason="opted out")
    assert store.is_suppressed(linkedin_url=checked)[0]


def test_domain_suppression_blocks_a_colleague(config):
    """
    "Stop contacting anyone here" must actually stop everyone there -- mailing
    a colleague after that is what gets a sending domain blacklisted.
    """
    suppression.for_tenant(EXAMPLE).add(
        domain="lakeside.com", reason="company-wide opt-out"
    )
    state = compliant_lead(contact_email="someone.else@lakeside.com")
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"


def test_tenant_blocked_domain_blocks(config):
    state = compliant_lead(contact_email="someone@gmail.com")
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"
    assert "blocked_domains" in update["next_action"]


def test_process_level_blocked_domain_rail(config):
    state = compliant_lead(contact_email="someone@example.com")
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"


def test_a_clear_lead_passes(config):
    update = n5_5_suppression_gate(compliant_lead(), tenant_config=config)
    assert update["suppression_status"] == "clear"


# =========================================================================== #
# The gate is never cached
# =========================================================================== #

def test_the_gate_re_reads_the_list_on_every_call(config):
    """
    Section 4, N5.5: "on EVERY run ... no exceptions". A lead cleared before a
    suppression is added must be blocked immediately afterwards, with no
    process restart.
    """
    state = compliant_lead()
    assert n5_5_suppression_gate(state, tenant_config=config)["suppression_status"] == "clear"

    suppression.for_tenant(EXAMPLE).add(email=state["contact_email"], reason="just now")

    assert n5_5_suppression_gate(state, tenant_config=config)["suppression_status"] == (
        "blocked_optout"
    )


def test_a_later_cadence_touch_is_gated_again(config):
    state = compliant_lead()
    state["sequence_step"] = 1
    suppression.for_tenant(EXAMPLE).add(email=state["contact_email"], reason="opted out")
    assert n5_5_suppression_gate(state, tenant_config=config)["suppression_status"] == (
        "blocked_optout"
    )


# =========================================================================== #
# Compliance rules
# =========================================================================== #

def test_missing_optout_line_blocks(config):
    state = compliant_lead(body=f"Hi Dana,\n\nNo booking system.\n\n{SIGNATURE}")
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_compliance"
    assert "opt-out" in update["manual_review_reason"]


def test_missing_sender_identification_blocks(config):
    state = compliant_lead(
        body="Hi Dana,\n\nNo booking system.\n\nreply unsubscribe to opt out.\n"
             "30 N Gould St, Sheridan, WY 82801"
    )
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_compliance"
    assert "identified" in update["manual_review_reason"]


def test_us_requires_a_physical_address(config):
    state = compliant_lead(
        body=f"Hi Dana,\n\nNo booking system.\n\n{SIGNATURE}\n"
             'Reply "unsubscribe" to opt out.'
    )
    update = n5_5_suppression_gate(state, tenant_config=config)
    assert update["suppression_status"] == "blocked_compliance"
    assert "physical postal address" in update["manual_review_reason"]


def test_uk_does_not_require_a_physical_address(config):
    state = compliant_lead(
        region="UK",
        contact_email="hello@bright.co.uk",
        body=f"Hi Priya,\n\nNo booking system.\n\n{SIGNATURE}\n"
             'Reply "unsubscribe" to opt out.',
    )
    assert check_compliance(state, config)[0]


def test_eu_requires_a_legal_basis_line(config):
    state = compliant_lead(
        region="EU", niche_id="local_salon",
        contact_email="contact@atelier.fr",
        body=f"Bonjour,\n\nvotre page de reservation.\n\n{SIGNATURE}\n"
             "Pour ne plus recevoir ces messages, repondez a ce message.",
    )
    passed, failures = check_compliance(state, config)
    assert not passed
    assert any("legal-basis" in f for f in failures)


def test_eu_passes_with_a_legal_basis_line(config):
    state = compliant_lead(
        region="EU", niche_id="local_salon",
        contact_email="contact@atelier.fr",
        body=f"Bonjour,\n\nvotre page de reservation.\n\n{SIGNATURE}\n"
             "Base legale: interet legitime (RGPD art. 6.1.f).\n"
             "Pour ne plus recevoir ces messages, repondez a ce message.",
    )
    passed, failures = check_compliance(state, config)
    assert passed, failures


def test_eu_blocks_a_personal_address(config):
    state = compliant_lead(
        region="EU", niche_id="local_salon",
        contact_email="camille.berthier@atelier.fr",
        body=f"Bonjour,\n\nreservation.\n\n{SIGNATURE}\n"
             "Base legale: interet legitime (RGPD).\n"
             "Pour ne plus recevoir ces messages, repondez.",
    )
    passed, failures = check_compliance(state, config)
    assert not passed
    assert any("role-based" in f for f in failures)


def test_french_optout_phrase_satisfies_the_eu_rule(config):
    """A localised draft must still pass; the profile lists FR phrases."""
    state = compliant_lead(
        region="EU", niche_id="local_salon", contact_email="contact@atelier.fr",
        body=f"Bonjour,\n\nreservation.\n\n{SIGNATURE}\n"
             "Base legale: interet legitime (RGPD).\n"
             "Si vous ne souhaitez plus recevoir ces messages, dites-le moi.",
    )
    passed, failures = check_compliance(state, config)
    assert passed, failures


def test_region_touch_ceiling_blocks_an_extra_touch(config):
    """EU caps at 2 touches; a third must not go out."""
    state = compliant_lead(
        region="EU", niche_id="local_salon", contact_email="contact@atelier.fr",
        body=f"Bonjour,\n\nreservation.\n\n{SIGNATURE}\n"
             "Base legale: interet legitime (RGPD).\n"
             "Pour ne plus recevoir ces messages, repondez.",
    )
    state["sequence_step"] = 2          # the next touch would be #3
    passed, failures = check_compliance(state, config)
    assert not passed
    assert any("exceeds the ceiling" in f for f in failures)


def test_linkedin_only_lead_is_not_asked_for_an_email_optout_line(config):
    """LinkedIn has its own platform opt-out; the email rules do not apply."""
    state = compliant_lead(
        channel="linkedin", contact_email="",
        linkedin_url="https://linkedin.com/company/lakeside",
    )
    state["draft_message"] = {
        "linkedin": {
            "connection_note": f"Hi Dana - noticed no online booking. {SIGNATURE}.",
        }
    }
    assert check_compliance(state, config)[0]


# =========================================================================== #
# Auto-suppression from replies
# =========================================================================== #

@pytest.mark.parametrize(
    "category,text",
    [
        ("not_interested", "No thanks."),
        ("no_reply", "Please remove me from your list."),
        ("interested", "unsubscribe"),                    # text beats the label
        ("objection", "Where did you get my email address?"),
        ("objection", "Please tell me your legal basis under GDPR."),
        ("not_interested", "Nicht interessiert, bitte abmelden."),
        ("no_reply", "Merci de ne plus recevoir vos messages."),
    ],
)
def test_replies_that_must_auto_suppress(category, text):
    should, reason = should_auto_suppress(category, text)
    assert should, f"{category} / {text!r} must suppress"
    assert reason


@pytest.mark.parametrize(
    "category,text",
    [
        ("interested", "Sounds good, can you do Thursday at 2?"),
        ("out_of_office", "I am out of the office until 14 March."),
        ("objection", "We already have a vendor for this."),
        ("no_reply", ""),
    ],
)
def test_replies_that_must_not_auto_suppress(category, text):
    should, _ = should_auto_suppress(category, text)
    assert not should


def test_auto_suppress_writes_to_the_list_without_operator_action(config):
    """
    Section 4, N5.5: "this must not depend on the human operator remembering".
    """
    state = compliant_lead()
    state["reply_category"] = "not_interested"
    state["reply_text"] = "Not interested, please remove me."

    store = suppression.for_tenant(EXAMPLE)
    assert not store.is_suppressed(email=state["contact_email"])[0]

    update = auto_suppress_from_reply(state)
    assert update["suppression_status"] == "blocked_optout"
    assert update["archived"] is True

    fresh = suppression.for_tenant(EXAMPLE)
    assert fresh.is_suppressed(email=state["contact_email"])[0]


def test_auto_suppress_also_records_the_linkedin_url(config):
    state = compliant_lead(
        channel="both", linkedin_url="https://www.linkedin.com/in/dana/"
    )
    state["reply_category"] = "not_interested"
    state["reply_text"] = "Not interested."
    auto_suppress_from_reply(state)

    store = suppression.for_tenant(EXAMPLE)
    assert store.is_suppressed(linkedin_url="https://linkedin.com/in/dana")[0]


def test_auto_suppress_is_a_no_op_for_a_positive_reply(config):
    state = compliant_lead()
    state["reply_category"] = "interested"
    state["reply_text"] = "Yes, Thursday works."
    assert auto_suppress_from_reply(state) == {}
    assert not suppression.for_tenant(EXAMPLE).is_suppressed(
        email=state["contact_email"]
    )[0]


def test_auto_suppressed_contact_is_blocked_on_the_next_touch(config):
    """The whole chain: reply -> auto-suppress -> next touch blocked."""
    state = compliant_lead()
    state["reply_category"] = "not_interested"
    state["reply_text"] = "Please unsubscribe me."
    auto_suppress_from_reply(state)

    next_touch = compliant_lead()
    next_touch["sequence_step"] = 1
    update = n5_5_suppression_gate(next_touch, tenant_config=config)
    assert update["suppression_status"] == "blocked_optout"


# =========================================================================== #
# Suppression list mechanics
# =========================================================================== #

def test_add_is_idempotent():
    store = suppression.for_tenant("tenant_idem")
    store.add(email="a@b.com", reason="first")
    store.add(email="a@b.com", reason="second")
    assert len(store) == 1
    assert "second" in store.is_suppressed(email="a@b.com")[1]


def test_remove_undoes_a_mistake():
    store = suppression.for_tenant("tenant_rm")
    store.add(email="a@b.com", reason="oops")
    assert store.remove(email="a@b.com")
    assert not store.is_suppressed(email="a@b.com")[0]


def test_entries_are_listable_for_the_operator():
    store = suppression.for_tenant("tenant_list")
    store.add(email="a@b.com", reason="one")
    store.add(linkedin_url="https://linkedin.com/in/x", reason="two")
    reasons = {e["reason"] for e in store.entries()}
    assert reasons == {"one", "two"}


def test_a_crm_mirror_failure_does_not_stop_suppression(monkeypatch):
    """The local file is the source of truth; a Sheets outage cannot block it."""
    monkeypatch.setenv("SUPPRESSION_BACKEND", "both")

    def explode(*args, **kwargs):
        raise RuntimeError("sheets down")

    monkeypatch.setattr("src.integrations.sheets_crm.append_suppression", explode)
    store = suppression.for_tenant("tenant_mirror")
    store.add(email="a@b.com", reason="opted out")
    assert store.is_suppressed(email="a@b.com")[0]


# =========================================================================== #
# Routing
# =========================================================================== #

@pytest.mark.parametrize(
    "status,channel,expected",
    [
        ("clear", "email", "email_outreach"),
        ("clear", "linkedin", "linkedin_outreach"),
        ("clear", "both", "email_outreach"),
        ("blocked_optout", "email", "archive"),
        ("blocked_compliance", "linkedin", "archive"),
        ("clear", "", "archive"),
    ],
)
def test_route_after_suppression(status, channel, expected):
    state = compliant_lead()
    state["suppression_status"] = status
    state["channel"] = channel
    assert route_after_suppression(state) == expected
