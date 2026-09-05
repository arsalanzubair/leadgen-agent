"""
Tests for the LeadState data contract (Section 3 / Section 9).

These are deliberately strict: state.py is the contract the whole system is
built around, so a silent rename or a dropped field must fail here first.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.state import (
    APPROVED_STATUSES,
    AUTO_SUPPRESS_CATEGORIES,
    CRM_COLUMNS,
    EXITS_SEQUENCE_CATEGORIES,
    FREE_TIER_LIMITS,
    MAX_LINKEDIN_CONNECTION_NOTE_CHARS,
    REGION_VALUES,
    ApprovalStatus,
    Channel,
    LeadState,
    Region,
    ReplyCategory,
    SendStatus,
    SuppressionStatus,
    is_reachable,
    lead_to_crm_dict,
    lead_to_crm_row,
    make_dedupe_key,
    make_lead_id,
    new_lead_state,
    normalise_domain,
    record_error,
    touch,
    utcnow,
    validate_lead_state,
    wants_email,
    wants_linkedin,
)

# --------------------------------------------------------------------------- #
# Schema shape
# --------------------------------------------------------------------------- #

#: The Section 3 contract, verbatim. If a field here disappears or is renamed,
#: every downstream node and the CRM sheet break -- so assert it explicitly.
CONTRACT_FIELDS = {
    # identity
    "tenant_id", "niche_id", "region", "language", "lead_id",
    # company
    "company_name", "website", "linkedin_url", "location", "industry",
    # contact
    "contact_email", "contact_name",
    # research
    "signals",
    # qualification
    "fit_score", "fit_reason",
    # outreach
    "channel", "draft_message", "approval_status", "suppression_status",
    "send_status",
    # sequencing
    "sequence_step", "next_action",
    # replies
    "reply_text", "reply_category",
    # meta
    "last_updated",
}


def test_contract_fields_all_present_in_annotations():
    missing = CONTRACT_FIELDS - set(LeadState.__annotations__)
    assert not missing, f"LeadState is missing contract fields: {sorted(missing)}"


def test_new_lead_state_populates_every_contract_field():
    state = new_lead_state(
        tenant_id="t1", niche_id="local_dental", region=Region.UK,
        company_name="Bright Smile Dental", location="Manchester, UK",
    )
    missing = CONTRACT_FIELDS - set(state)
    assert not missing, f"new_lead_state left fields unset: {sorted(missing)}"


def test_new_lead_state_defaults_are_sane():
    state = new_lead_state(tenant_id="t1", niche_id="local_dental", region="UK")
    assert state["fit_score"] == 0
    assert state["sequence_step"] == 0
    assert state["signals"] == []
    assert state["approval_status"] == ApprovalStatus.PENDING.value
    assert state["suppression_status"] == SuppressionStatus.CLEAR.value
    assert state["send_status"] == SendStatus.NOT_SENT.value
    assert state["reply_category"] == ReplyCategory.NO_REPLY.value
    assert state["unreachable"] is False
    assert state["archived"] is False
    assert state["errors"] == []


def test_enums_are_stored_as_plain_strings_not_enum_objects():
    """Checkpoints, Sheets rows and JSON fixtures must round-trip identically."""
    state = new_lead_state(tenant_id="t1", niche_id="n", region=Region.ME)
    for key in ("region", "channel", "approval_status", "suppression_status",
                "send_status", "reply_category"):
        assert type(state[key]) is str, f"{key} stored as {type(state[key])}"


def test_all_six_regions_supported():
    assert REGION_VALUES == {"US", "UK", "EU", "CA", "AU", "ME"}


def test_free_tier_limits_match_documented_ceilings():
    assert FREE_TIER_LIMITS["hunter_lookups_per_month"] == 25
    assert FREE_TIER_LIMITS["gmail_sends_per_day"] == 500
    assert FREE_TIER_LIMITS["brevo_sends_per_day"] == 300
    assert MAX_LINKEDIN_CONNECTION_NOTE_CHARS == 300


# --------------------------------------------------------------------------- #
# Identity / dedupe
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://WWW.Acme.co.uk/about?x=1", "acme.co.uk"),
        ("http://acme.co.uk", "acme.co.uk"),
        ("acme.co.uk/", "acme.co.uk"),
        ("https://sub.acme.com/a/b#frag", "sub.acme.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_domain(url, expected):
    assert normalise_domain(url) == expected


def test_dedupe_prefers_domain_over_name():
    a = make_dedupe_key("Bright Smile Dental", "Manchester", "https://bright-smile.co.uk")
    b = make_dedupe_key("Bright Smile Dental Ltd", "Manchester UK", "http://www.bright-smile.co.uk/contact")
    assert a == b == "domain:bright-smile.co.uk"


def test_dedupe_falls_back_to_name_and_location():
    a = make_dedupe_key("  Bright   Smile  ", "Manchester", "")
    b = make_dedupe_key("BRIGHT SMILE", "manchester", "")
    assert a == b
    assert a.startswith("name:")


def test_lead_id_is_deterministic_and_tenant_scoped():
    key = make_dedupe_key("Acme", "London", "https://acme.com")
    assert make_lead_id("tenant_a", key) == make_lead_id("tenant_a", key)
    assert make_lead_id("tenant_a", key) != make_lead_id("tenant_b", key)
    assert make_lead_id("tenant_a", key).startswith("tenant_a-")


def test_rediscovering_the_same_company_yields_the_same_lead_id():
    kw = dict(tenant_id="t1", niche_id="local_dental", region="UK")
    first = new_lead_state(company_name="Acme", website="https://acme.com", **kw)
    again = new_lead_state(company_name="ACME", website="http://www.acme.com/x", **kw)
    assert first["lead_id"] == again["lead_id"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def test_utcnow_is_timezone_aware():
    assert utcnow().tzinfo is not None


def test_touch_always_refreshes_last_updated():
    update = touch(fit_score=82, fit_reason="strong")
    assert update["fit_score"] == 82
    assert isinstance(update["last_updated"], datetime)
    assert update["last_updated"].tzinfo is not None


def test_record_error_appends_without_mutating():
    state = new_lead_state(tenant_id="t1", niche_id="n", region="US")
    errors = record_error(state, "n3_qualification", ValueError("llm timeout"))
    assert state["errors"] == []           # original untouched
    assert len(errors) == 1
    assert errors[0]["node"] == "n3_qualification"
    assert "ValueError: llm timeout" == errors[0]["error"]


def test_is_reachable_requires_email_or_linkedin():
    base = dict(tenant_id="t1", niche_id="n", region="US")
    assert not is_reachable(new_lead_state(**base))
    assert is_reachable(new_lead_state(contact_email="a@b.com", **base))
    assert is_reachable(new_lead_state(linkedin_url="https://linkedin.com/in/x", **base))


@pytest.mark.parametrize(
    "channel,email,linkedin",
    [
        (Channel.EMAIL, True, False),
        (Channel.LINKEDIN, False, True),
        (Channel.BOTH, True, True),
        ("email", True, False),
        ("both", True, True),
    ],
)
def test_channel_predicates(channel, email, linkedin):
    assert wants_email(channel) is email
    assert wants_linkedin(channel) is linkedin


# --------------------------------------------------------------------------- #
# Routing constants
# --------------------------------------------------------------------------- #

def test_edited_counts_as_approved():
    """A human who rewrote the draft has signed off on it (N5 -> N5.5)."""
    assert APPROVED_STATUSES == {"approved", "edited"}
    assert "rejected" not in APPROVED_STATUSES
    assert "pending" not in APPROVED_STATUSES


def test_not_interested_auto_suppresses_but_objection_does_not():
    assert "not_interested" in AUTO_SUPPRESS_CATEGORIES
    assert "objection" not in AUTO_SUPPRESS_CATEGORIES


def test_interested_reply_exits_the_sequence():
    assert "interested" in EXITS_SEQUENCE_CATEGORIES
    assert "out_of_office" not in EXITS_SEQUENCE_CATEGORIES
    assert "no_reply" not in EXITS_SEQUENCE_CATEGORIES


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_valid_state_has_no_problems():
    state = new_lead_state(
        tenant_id="t1", niche_id="local_dental", region="UK",
        company_name="Bright Smile Dental",
    )
    assert validate_lead_state(state) == []


def test_missing_required_field_is_named():
    state = new_lead_state(tenant_id="t1", niche_id="n", region="US")
    problems = validate_lead_state(state)
    assert any("company_name" in p for p in problems)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("region", "APAC"),
        ("channel", "sms"),
        ("approval_status", "maybe"),
        ("suppression_status", "blocked"),
        ("send_status", "queued"),
        ("reply_category", "positive"),
    ],
)
def test_invalid_enum_values_are_rejected(field, bad):
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    state[field] = bad
    problems = validate_lead_state(state)
    assert any(field in p for p in problems), problems


@pytest.mark.parametrize("score", [-1, 101, 3.5, True, "80"])
def test_fit_score_range_and_type(score):
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    state["fit_score"] = score
    assert any("fit_score" in p for p in validate_lead_state(state))


def test_fit_score_boundaries_are_valid():
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    for score in (0, 50, 100):
        state["fit_score"] = score
        assert validate_lead_state(state) == []


def test_over_length_linkedin_connection_note_is_rejected():
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    state["draft_message"] = {
        "linkedin": {"connection_note": "x" * (MAX_LINKEDIN_CONNECTION_NOTE_CHARS + 1)}
    }
    problems = validate_lead_state(state)
    assert any("connection_note" in p for p in problems)

    state["draft_message"] = {
        "linkedin": {"connection_note": "x" * MAX_LINKEDIN_CONNECTION_NOTE_CHARS}
    }
    assert validate_lead_state(state) == []


def test_naive_datetime_is_rejected():
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    state["last_updated"] = datetime(2026, 1, 1, 12, 0, 0)   # no tzinfo
    assert any("timezone-aware" in p for p in validate_lead_state(state))


def test_validate_returns_problems_rather_than_raising():
    """Section 8: one bad lead must not take the batch down."""
    assert isinstance(validate_lead_state({}), list)


# --------------------------------------------------------------------------- #
# CRM projection (N9 / Looker Studio contract)
# --------------------------------------------------------------------------- #

def test_crm_columns_are_unique_and_cover_the_contract():
    assert len(CRM_COLUMNS) == len(set(CRM_COLUMNS))
    # reply_text and everything else operators filter on must be present
    for col in ("lead_id", "tenant_id", "region", "fit_score", "channel",
                "send_status", "reply_category", "last_updated"):
        assert col in CRM_COLUMNS


def test_crm_row_is_all_flat_strings():
    state = new_lead_state(
        tenant_id="t1", niche_id="local_dental", region="UK",
        company_name="Bright Smile", website="https://bright-smile.co.uk",
    )
    state["signals"] = ["no online booking", "hiring 2 hygienists"]
    state["fit_score"] = 84
    row = lead_to_crm_row(state)
    assert len(row) == len(CRM_COLUMNS)
    assert all(isinstance(cell, str) for cell in row)


def test_crm_row_formatting_rules():
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="US", company_name="Acme"
    )
    state["signals"] = ["a", "b"]
    state["unreachable"] = True
    state["sent_at"] = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)
    row = lead_to_crm_dict(state)
    assert row["signals"] == "a | b"                 # lists flattened
    assert row["unreachable"] == "TRUE"              # booleans Looker-friendly
    assert row["sent_at"].startswith("2026-03-01T")  # ISO-8601 timestamps
    assert row["last_touch_at"] == ""                # None becomes empty cell


def test_crm_dict_and_row_agree():
    state = new_lead_state(
        tenant_id="t1", niche_id="n", region="AU", company_name="Acme"
    )
    as_dict = lead_to_crm_dict(state)
    as_row = lead_to_crm_row(state)
    assert [as_dict[c] for c in CRM_COLUMNS] == as_row
