"""
The "USA" case: a real, supported place named the way a person actually
spells it rather than the workspace's region code.

Reported failure: "find dental clinics in USA that may need voice agents"
was accepted at the parse step (a real category is named) but then rejected
at start with a raw, internal message --

    tenant config .../example_tenant.yaml is invalid; halting before
    Discovery. - niches[...].discovery: missing 'locations'

-- because `draft_niche` keyed `locations` by whatever the model returned
("USA") and silently dropped it for not matching the workspace's region code
("US") exactly, leaving an empty `locations` dict that the file validator
then rejects. Two distinct problems, both covered here:

  1. `_normalise_region` should recognise the common ways a person spells one
     of this workspace's regions, so a named, supported place is not lost.
  2. Whatever internal `ConfigError` still gets raised must never reach a
     user as the file validator's own wording -- `workspace.friendly_config_error`
     is the one place that turns it into something actionable.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend import workspace
from backend.routers import niches as niches_router
from backend.routers import runs as runs_router
from src.reliability import ConfigError

REGIONS = ["US", "UK", "EU", "CA", "AU", "ME"]


# --------------------------------------------------------------------------- #
# _normalise_region: the spelling a person actually uses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("USA", "US"), ("usa", "US"), ("U.S.", "US"), ("United States", "US"),
        ("america", "US"), ("US", "US"),
        ("UK", "UK"), ("United Kingdom", "UK"), ("Britain", "UK"), ("england", "UK"),
        ("EU", "EU"), ("europe", "EU"), ("European Union", "EU"),
        ("Canada", "CA"), ("ca", "CA"),
        ("Australia", "AU"), ("au", "AU"),
        ("UAE", "ME"), ("Saudi Arabia", "ME"), ("middle east", "ME"),
    ],
)
def test_common_spellings_resolve_to_the_workspace_region(spelling, expected):
    assert niches_router._normalise_region(spelling, REGIONS) == expected


def test_a_region_this_workspace_does_not_operate_in_does_not_match():
    assert niches_router._normalise_region("Japan", REGIONS) is None
    assert niches_router._normalise_region("Germany", ["US"]) is None


def test_blank_input_does_not_match():
    assert niches_router._normalise_region("", REGIONS) is None
    assert niches_router._normalise_region("   ", REGIONS) is None


# --------------------------------------------------------------------------- #
# draft_niche: the model's spelling must not be silently discarded
# --------------------------------------------------------------------------- #


def _mock_llm(monkeypatch, parsed: dict):
    monkeypatch.setattr(niches_router.llm, "active_provider", lambda: "gemini")
    monkeypatch.setattr(
        niches_router.llm, "complete_json", lambda *a, **k: (parsed, None)
    )
    monkeypatch.setattr(
        niches_router.workspace, "read_rules",
        lambda tenant_id: {"regions": REGIONS},
    )
    monkeypatch.setattr(niches_router.workspace, "resolve_tenant_id", lambda: "t1")


def test_the_exact_reported_case_usa_is_not_dropped(monkeypatch):
    _mock_llm(monkeypatch, {
        "label": "dental clinics", "kind": "local_business",
        "search_terms": ["dental clinic"], "titles": [],
        "locations": {"USA": ["United States"]},
        "good_signals": ["no online booking"], "interpretation": "Dental clinics in the US.",
    })
    draft = niches_router.draft_niche(niches_router.DraftRequest(
        description="find dental clinics in USA that may need voice agents"
    ))
    assert draft["locations"] == {"US": ["United States"]}
    assert draft["unmatched_locations"] == []


def test_a_genuinely_unsupported_place_is_reported_not_silently_dropped(monkeypatch):
    _mock_llm(monkeypatch, {
        "label": "dental clinics", "kind": "local_business",
        "search_terms": ["dental clinic"], "titles": [],
        "locations": {"Japan": ["Tokyo"]},
        "good_signals": ["no online booking"], "interpretation": "Dental clinics in Japan.",
    })
    draft = niches_router.draft_niche(niches_router.DraftRequest(
        description="find dental clinics in Japan"
    ))
    assert draft["locations"] == {}
    assert draft["unmatched_locations"] == ["Tokyo"]


# --------------------------------------------------------------------------- #
# parse_request: a named-but-unsupported place is refused honestly, not
# silently turned into "search every region this workspace operates in"
# --------------------------------------------------------------------------- #


def _draft(**overrides):
    base = {
        "id": "", "label": "", "kind": "local_business",
        "search_terms": [], "titles": [], "locations": {},
        "description": "", "must_have": [], "good_signals": [],
        "disqualifiers": [], "channel_default": "email", "tone": "",
        "size_hint": "", "interpretation": "", "unmatched_locations": [],
    }
    base.update(overrides)
    return base


def test_parse_accepts_the_reported_query_once_usa_normalises(monkeypatch):
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(
            label="dental clinics", search_terms=["dental clinic"],
            locations={"US": ["United States"]},
        ),
    )
    plan = runs_router.parse_request(
        runs_router.ParseRequest(prompt="find dental clinics in USA that may need voice agents")
    )
    assert plan["locations"] == ["United States"]
    assert plan["regions"] == ["US"]


def test_parse_refuses_a_named_but_unsupported_region_honestly(monkeypatch):
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(
            label="dental clinics", search_terms=["dental clinic"],
            locations={}, unmatched_locations=["Tokyo"],
        ),
    )
    monkeypatch.setattr(
        runs_router.workspace, "read_rules",
        lambda tenant_id: {"regions": REGIONS},
    )
    with pytest.raises(HTTPException) as caught:
        runs_router.parse_request(runs_router.ParseRequest(prompt="dental clinics in Japan"))
    assert caught.value.status_code == 422
    detail = str(caught.value.detail)
    assert "Tokyo" in detail
    assert "doesn't search" in detail


# --------------------------------------------------------------------------- #
# The internal validator's own wording must never reach a user
# --------------------------------------------------------------------------- #


def test_the_files_validator_message_is_translated_not_leaked():
    raw = ConfigError(
        "tenant config /opt/render/project/src/config/tenants/example_tenant.yaml "
        "is invalid; halting before Discovery.\n"
        "  - niches[dental_clinics_in_the_usa].discovery: missing 'locations'"
    )
    friendly = workspace.friendly_config_error(raw)
    assert "tenant config" not in friendly
    assert "niches[" not in friendly
    assert ".yaml" not in friendly
    assert "place to search" in friendly


def test_a_hand_written_workspace_message_passes_through_unchanged():
    raw = ConfigError("there is already an audience called 'dental_clinics'. Give this one a different name.")
    assert workspace.friendly_config_error(raw) == str(raw)


def test_start_run_never_leaks_the_validator_message(monkeypatch):
    """
    Belt and braces on the exact path from the report: a niche saved with
    empty locations (however that happened) must surface a plain message at
    POST /api/runs, not the file validator's own wording.
    """
    monkeypatch.setattr(runs_router.workspace, "resolve_tenant_id", lambda: "t1")
    monkeypatch.setattr(runs_router.workspace, "read_niches", lambda tenant_id: [])

    def _boom(tenant_id, payload, *, creating):
        raise ConfigError(
            "tenant config /opt/render/project/src/config/tenants/example_tenant.yaml "
            "is invalid; halting before Discovery.\n"
            "  - niches[x].discovery: missing 'locations'"
        )

    monkeypatch.setattr(runs_router.workspace, "save_niche", _boom)

    with pytest.raises(HTTPException) as caught:
        runs_router.start_run(runs_router.StartRequest(
            prompt="find dental clinics in USA",
            config={"niche_ids": ["x"]},
            niche_draft={"id": "x", "label": "x", "locations": {}},
        ))
    detail = str(caught.value.detail)
    assert "tenant config" not in detail
    assert "niches[" not in detail
