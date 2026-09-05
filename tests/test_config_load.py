"""
N0 tenant config load tests (Build step 2).

The central requirement being proved here is Section 4's "never silently
default": a config missing a required field must HALT with that field named,
not proceed with a plausible-looking guess.
"""

from __future__ import annotations

import copy

import pytest
import yaml

from src.nodes.n0_config_load import (
    TenantConfig,
    _merge_cadences,
    load_tenant_config,
    n0_config_load,
    resolve_batch_targets,
)
from src.reliability import ConfigError
from src.settings import ROOT
from src.state import new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _clear_config_cache():
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def example_raw() -> dict:
    path = ROOT / "config" / "tenants" / "example_tenant.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_tenant(tmp_path, monkeypatch, raw: dict, tenant_id: str) -> None:
    """Point the loader at a throwaway config directory containing `raw`."""
    tenants = tmp_path / "config" / "tenants"
    tenants.mkdir(parents=True, exist_ok=True)
    (tenants / f"{tenant_id}.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8"
    )
    monkeypatch.setattr(
        "src.nodes.n0_config_load.tenant_config_path",
        lambda tid: tenants / f"{tid}.yaml",
    )


# --------------------------------------------------------------------------- #
# The shipped example must be valid
# --------------------------------------------------------------------------- #

def test_example_tenant_loads():
    config = load_tenant_config(EXAMPLE)
    assert isinstance(config, TenantConfig)
    assert config.tenant_id == EXAMPLE
    assert set(config.niche_ids) == {
        "local_dental", "local_salon", "local_gym", "b2b_saas_ops"
    }
    assert config.fit_score_threshold == 60
    assert "email" in config.enabled_channels
    assert "linkedin" in config.enabled_channels


def test_every_configured_region_has_a_compliance_profile():
    config = load_tenant_config(EXAMPLE)
    for region in config.regions:
        profile = config.compliance_profile(region)
        # Defaults must have been merged underneath every region.
        assert "requires_optout_line" in profile
        assert "optout_phrases" in profile
        assert "business_hours" in profile


def test_language_map_covers_every_region():
    config = load_tenant_config(EXAMPLE)
    for region in config.regions:
        assert config.language_for(region)
    assert config.language_for("EU") == "fr"


def test_cadences_are_available_for_every_enabled_channel():
    config = load_tenant_config(EXAMPLE)
    for channel in ("email", "linkedin", "both"):
        cadence = config.cadence_for(channel)
        assert cadence["steps"], f"{channel} cadence has no steps"


def test_tenant_cadence_override_is_merged_not_replaced():
    """The tenant overrides only wait_days on step 2; intent must survive."""
    config = load_tenant_config(EXAMPLE)
    step2 = config.cadence_for("email")["steps"][1]
    assert step2["wait_days"] == 3          # tenant override (default is 4)
    assert step2["intent"]                  # default intent text preserved
    assert step2["name"] == "value_nudge"


def test_merge_cadences_merges_by_step_number():
    defaults = {
        "email": {"steps": [
            {"step": 1, "name": "opener", "wait_days": 0, "intent": "keep me"},
            {"step": 2, "name": "nudge", "wait_days": 4, "intent": "keep me too"},
        ]}
    }
    merged = _merge_cadences(defaults, {"email": {"steps": [{"step": 2, "wait_days": 1}]}})
    assert merged["email"]["steps"][1]["wait_days"] == 1
    assert merged["email"]["steps"][1]["intent"] == "keep me too"
    assert merged["email"]["steps"][0]["wait_days"] == 0


def test_max_touches_takes_the_stricter_of_cadence_and_region():
    config = load_tenant_config(EXAMPLE)
    # EU compliance caps at 2; the email cadence is 3 steps long.
    assert config.max_touches("email", "EU") == 2
    # US caps at 4; the cadence is the binding constraint at 3.
    assert config.max_touches("email", "US") == 3


# --------------------------------------------------------------------------- #
# Halt-on-missing-field
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "dotted",
    [
        "regions",
        "niches",
        "tone",
        "fit_score_threshold",
        "daily_send_limits",
        "language_map",
    ],
)
def test_missing_top_level_field_halts_and_names_it(
    tmp_path, monkeypatch, example_raw, dotted
):
    raw = copy.deepcopy(example_raw)
    raw.pop(dotted)
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError) as excinfo:
        load_tenant_config(EXAMPLE)
    assert dotted in str(excinfo.value)


@pytest.mark.parametrize(
    "dotted,parent,key",
    [
        ("channels.enabled", "channels", "enabled"),
        ("sending_identity.from_name", "sending_identity", "from_name"),
        ("sending_identity.from_email", "sending_identity", "from_email"),
        ("sending_identity.company_name", "sending_identity", "company_name"),
        ("sending_identity.linkedin_account_label", "sending_identity",
         "linkedin_account_label"),
        ("compliance.profile_set", "compliance", "profile_set"),
    ],
)
def test_missing_nested_field_halts_and_names_it(
    tmp_path, monkeypatch, example_raw, dotted, parent, key
):
    raw = copy.deepcopy(example_raw)
    raw[parent].pop(key)
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError) as excinfo:
        load_tenant_config(EXAMPLE)
    assert dotted in str(excinfo.value)


def test_all_problems_are_reported_at_once(tmp_path, monkeypatch, example_raw):
    """One pass to fix the config, not one rerun per missing field."""
    raw = copy.deepcopy(example_raw)
    raw.pop("tone")
    raw.pop("regions")
    raw["sending_identity"].pop("from_email")
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError) as excinfo:
        load_tenant_config(EXAMPLE)
    message = str(excinfo.value)
    assert "tone" in message
    assert "regions" in message
    assert "sending_identity.from_email" in message


def test_missing_config_file_names_the_expected_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.nodes.n0_config_load.tenant_config_path",
        lambda tid: tmp_path / f"{tid}.yaml",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_tenant_config("ghost_tenant")
    assert "ghost_tenant" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Value validation
# --------------------------------------------------------------------------- #

def test_unsupported_region_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["regions"].append("APAC")
    raw["language_map"]["APAC"] = "en"
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="APAC"):
        load_tenant_config(EXAMPLE)


def test_region_without_language_map_entry_is_rejected(
    tmp_path, monkeypatch, example_raw
):
    raw = copy.deepcopy(example_raw)
    raw["language_map"].pop("ME")
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="language_map"):
        load_tenant_config(EXAMPLE)


def test_tenant_id_must_match_filename(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["tenant_id"] = "someone_else"
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="mismatch"):
        load_tenant_config(EXAMPLE)


def test_invalid_channel_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["channels"]["enabled"] = ["email", "sms"]
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="sms"):
        load_tenant_config(EXAMPLE)


def test_out_of_range_threshold_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["fit_score_threshold"] = 140
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="fit_score_threshold"):
        load_tenant_config(EXAMPLE)


def test_duplicate_niche_id_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["niches"].append(copy.deepcopy(raw["niches"][0]))
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="duplicate niche id"):
        load_tenant_config(EXAMPLE)


def test_b2b_niche_without_titles_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    for niche in raw["niches"]:
        if niche["type"] == "b2b":
            niche["discovery"].pop("titles")
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="titles"):
        load_tenant_config(EXAMPLE)


def test_local_niche_without_search_terms_is_rejected(
    tmp_path, monkeypatch, example_raw
):
    raw = copy.deepcopy(example_raw)
    for niche in raw["niches"]:
        if niche["type"] == "local_business":
            niche["discovery"].pop("search_terms")
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="search_terms"):
        load_tenant_config(EXAMPLE)


def test_icp_without_good_signals_is_rejected(tmp_path, monkeypatch, example_raw):
    raw = copy.deepcopy(example_raw)
    raw["niches"][0]["icp"].pop("good_signals")
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    with pytest.raises(ConfigError, match="good_signals"):
        load_tenant_config(EXAMPLE)


# --------------------------------------------------------------------------- #
# Batch targets
# --------------------------------------------------------------------------- #

def test_resolve_batch_targets_skips_regions_with_no_locations():
    config = load_tenant_config(EXAMPLE)
    targets = resolve_batch_targets(config)
    pairs = {(t["niche_id"], t["region"]) for t in targets}
    # local_dental has no EU or ME locations configured.
    assert ("local_dental", "EU") not in pairs
    assert ("local_dental", "ME") not in pairs
    assert ("local_dental", "UK") in pairs
    # b2b_saas_ops covers every region.
    assert ("b2b_saas_ops", "ME") in pairs


def test_batch_targets_carry_the_regions_language():
    config = load_tenant_config(EXAMPLE)
    eu = [t for t in resolve_batch_targets(config) if t["region"] == "EU"]
    assert eu and all(t["language"] == "fr" for t in eu)


def test_zero_targets_halts(tmp_path, monkeypatch, example_raw):
    """
    Config is individually valid -- every niche declares locations -- but none
    of them overlap the regions the tenant operates in, so there is genuinely
    nothing to discover. That must halt rather than run an empty batch.
    """
    raw = copy.deepcopy(example_raw)
    raw["regions"] = ["US"]
    raw["language_map"] = {"US": "en"}
    for niche in raw["niches"]:
        niche["discovery"]["locations"] = {"UK": ["Manchester"]}
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    config = load_tenant_config(EXAMPLE)
    with pytest.raises(ConfigError, match="zero"):
        resolve_batch_targets(config)


# --------------------------------------------------------------------------- #
# The graph node itself
# --------------------------------------------------------------------------- #

def test_node_fills_language_from_the_region():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", language="",
    )
    assert n0_config_load(state)["language"] == "fr"


def test_node_leaves_an_explicit_language_alone():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", language="de",
    )
    assert "language" not in n0_config_load(state)


def test_node_halts_on_unknown_niche():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_taxidermy", region="US",
        company_name="Acme",
    )
    with pytest.raises(ConfigError, match="local_taxidermy"):
        n0_config_load(state)


def test_node_halts_on_a_region_the_tenant_does_not_operate_in(
    tmp_path, monkeypatch, example_raw
):
    raw = copy.deepcopy(example_raw)
    raw["regions"] = ["US"]
    raw["language_map"] = {"US": "en"}
    write_tenant(tmp_path, monkeypatch, raw, EXAMPLE)
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK", company_name="Acme"
    )
    with pytest.raises(ConfigError, match="UK"):
        n0_config_load(state)


def test_config_error_is_not_swallowed_by_the_node_decorator():
    """
    @node swallows everything EXCEPT ConfigError -- N0 must halt the batch
    rather than flag one lead for manual review.
    """
    state = new_lead_state(tenant_id="", niche_id="n", region="US", company_name="A")
    with pytest.raises(ConfigError):
        n0_config_load(state)
