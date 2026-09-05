"""
N3.5 channel selection rule tests (Build step 4).

The full truth table: niche default x data availability x tenant preference,
plus the two vetoes (tenant channel not enabled, region forbids the address we
have). No LLM is involved in this node, so these tests are exact -- every case
below has one correct answer, not a plausible one.
"""

from __future__ import annotations

import pytest

from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n3_5_channel_selection import (
    n3_5_channel_selection,
    niche_default_channel,
    select_channel,
)
from src.state import new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _clear_cache():
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


# --------------------------------------------------------------------------- #
# Niche defaults
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "niche_id,expected",
    [
        ("local_dental", "email"),
        ("local_salon", "email"),
        ("local_gym", "email"),
        ("b2b_saas_ops", "linkedin"),
        ("b2b_manufacturing", "linkedin"),
        ("saas_devtools", "linkedin"),
        ("unprefixed_niche", "email"),      # fallback
        ("", "email"),
    ],
)
def test_niche_default_channel(niche_id, expected):
    assert niche_default_channel(niche_id) == expected


# --------------------------------------------------------------------------- #
# Data availability overrides the niche default
# --------------------------------------------------------------------------- #

def test_email_only_forces_email_even_for_a_linkedin_first_niche():
    assert select_channel(
        niche_id="b2b_saas_ops", has_email=True, has_linkedin=False
    ) == "email"


def test_linkedin_only_forces_linkedin_even_for_an_email_first_niche():
    assert select_channel(
        niche_id="local_dental", has_email=False, has_linkedin=True
    ) == "linkedin"


def test_neither_available_yields_no_channel():
    assert select_channel(
        niche_id="local_dental", has_email=False, has_linkedin=False
    ) == ""


# --------------------------------------------------------------------------- #
# Both available -> tenant preference
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "preference,expected",
    [
        ("both", "both"),
        ("email", "email"),
        ("linkedin", "linkedin"),
        ("", "both"),                # unset defaults to both
        ("BOTH", "both"),            # case-insensitive
    ],
)
def test_both_available_uses_tenant_preference(preference, expected):
    assert select_channel(
        niche_id="local_dental", has_email=True, has_linkedin=True,
        preference_when_both=preference,
    ) == expected


@pytest.mark.parametrize(
    "niche_id,expected",
    [("local_dental", "email"), ("b2b_saas_ops", "linkedin")],
)
def test_preference_can_defer_to_the_niche_default(niche_id, expected):
    assert select_channel(
        niche_id=niche_id, has_email=True, has_linkedin=True,
        preference_when_both="niche_default",
    ) == expected


# --------------------------------------------------------------------------- #
# Tenant channel enablement
# --------------------------------------------------------------------------- #

def test_a_tenant_without_linkedin_never_gets_a_linkedin_touch():
    assert select_channel(
        niche_id="b2b_saas_ops", has_email=True, has_linkedin=True,
        enabled_channels=("email",),
    ) == "email"


def test_a_tenant_without_email_never_gets_an_email_touch():
    assert select_channel(
        niche_id="local_dental", has_email=True, has_linkedin=True,
        enabled_channels=("linkedin",),
    ) == "linkedin"


def test_disabled_channel_with_no_alternative_yields_nothing():
    assert select_channel(
        niche_id="local_dental", has_email=True, has_linkedin=False,
        enabled_channels=("linkedin",),
    ) == ""


# --------------------------------------------------------------------------- #
# Compliance veto on the email address
# --------------------------------------------------------------------------- #

def test_compliance_veto_falls_back_to_linkedin():
    assert select_channel(
        niche_id="b2b_saas_ops", has_email=True, has_linkedin=True,
        email_allowed=False,
    ) == "linkedin"


def test_compliance_veto_with_no_linkedin_yields_nothing():
    assert select_channel(
        niche_id="local_dental", has_email=True, has_linkedin=False,
        email_allowed=False,
    ) == ""


# --------------------------------------------------------------------------- #
# Exhaustive truth table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("niche_id", ["local_dental", "b2b_saas_ops"])
@pytest.mark.parametrize("has_email", [True, False])
@pytest.mark.parametrize("has_linkedin", [True, False])
@pytest.mark.parametrize("preference", ["both", "email", "linkedin"])
def test_truth_table_is_total_and_consistent(
    niche_id, has_email, has_linkedin, preference
):
    result = select_channel(
        niche_id=niche_id, has_email=has_email, has_linkedin=has_linkedin,
        preference_when_both=preference,
    )
    assert result in ("email", "linkedin", "both", "")

    # A channel is never chosen without the data to use it.
    if result in ("email", "both"):
        assert has_email
    if result in ("linkedin", "both"):
        assert has_linkedin
    # Data availability always beats preference when only one route exists.
    if has_email and not has_linkedin:
        assert result == "email"
    if has_linkedin and not has_email:
        assert result == "linkedin"
    if not has_email and not has_linkedin:
        assert result == ""


# --------------------------------------------------------------------------- #
# The node
# --------------------------------------------------------------------------- #

def test_node_sets_both_when_the_tenant_prefers_both(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US",
        company_name="Lakeside", contact_email="frontdesk@lakeside.com",
        linkedin_url="https://linkedin.com/company/lakeside",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "both"


def test_node_falls_back_to_email_when_no_linkedin_url(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="US",
        company_name="Grantly", contact_email="info@grantly.io",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "email"


def test_node_falls_back_to_linkedin_when_no_email(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", linkedin_url="https://linkedin.com/company/nordwind",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "linkedin"


def test_eu_personal_address_is_vetoed_to_linkedin(config):
    """The EU profile sets require_role_based_address: true."""
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", contact_email="katrin.vogel@nordwind.de",
        linkedin_url="https://linkedin.com/company/nordwind",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "linkedin"


def test_eu_role_based_address_is_allowed(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", contact_email="info@nordwind.de",
        linkedin_url="https://linkedin.com/company/nordwind",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "both"


def test_middle_east_personal_address_is_vetoed(config):
    """The ME profile also sets require_role_based_address: true."""
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_gym", region="ME",
        company_name="Peak Performance", contact_email="omar.alfahim@peak.ae",
    )
    update = n3_5_channel_selection(state, tenant_config=config)
    assert update["archived"] is True
    assert update["archive_reason"] == "no_channel"


def test_us_personal_address_is_fine(config):
    """CAN-SPAM has no role-based requirement."""
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US",
        company_name="Lakeside", contact_email="dana.whitfield@lakeside.com",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "email"


def test_node_archives_a_lead_with_no_route(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_salon", region="AU",
        company_name="Ivy & Oak",
    )
    update = n3_5_channel_selection(state, tenant_config=config)
    assert update["archived"] is True
    assert update["archive_reason"] == "no_channel"


def test_node_makes_no_llm_call(config, monkeypatch):
    """N3.5 is rule-based by design; an LLM call here would be a regression."""
    from src.integrations import llm

    def explode(*args, **kwargs):
        raise AssertionError("N3.5 must not call an LLM")

    monkeypatch.setattr(llm, "complete", explode)
    monkeypatch.setattr(llm, "complete_json", explode)

    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile", contact_email="hello@bright.co.uk",
    )
    assert n3_5_channel_selection(state, tenant_config=config)["channel"] == "email"
