"""
The "Germany" case: discovery criteria vs qualification criteria.

"Find businesses in Germany without AI-powered customer support" has a real
place (Germany) and a real qualifying trait (no AI-powered customer support),
but no real, searchable business category -- "businesses" is not a map
category or a job title.

Two bugs existed here at different times, and this file's history reflects
both:

  1. Originally, the drafting prompt had no rule against inventing a
     category, and `n1_discovery`'s local-search adapter looped over whatever
     category and location it was given with no check that either was
     non-empty -- the result was a search that silently found zero
     businesses, indistinguishable on screen from a provider failure or a
     thin region.
  2. The first fix for (1) made the backend REFUSE any request with no
     search_terms/titles at all, which overcorrected: "Germany" and "no
     AI-powered customer support" are both real, usable information, and
     rejecting the request lost them. The right behaviour is to accept it and
     run a broad, location-only search.

The current, correct behaviour, tested below:

  * `backend/routers/niches.py::DRAFT_SYSTEM` tells the model to leave
    `search_terms`/`titles` empty rather than invent a category when the
    description names no real one -- this file cannot call a real model, so
    it is verified by parametrized keyword assertions on the prompt text.
  * `backend/routers/runs.py::parse_request` accepts a request with a place
    but no category (broad search) or a category but no place (the tenant's
    configured regions apply), and refuses only a request with NEITHER --
    verified here directly, since it needs no real model to test.
  * `src/nodes/n1_discovery.py::discover_for_target` routes a local_business
    niche with no search_terms to company-level discovery instead of a map
    search -- covered separately in `tests/test_discovery_enrichment.py` and
    `tests/test_nodes_with_mocked_apis.py`, since it needs a real
    `TenantConfig` and a mocked provider, not just `parse_request`.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.routers import niches as niches_router
from backend.routers import runs as runs_router


def _draft(**overrides):
    base = {
        "id": "", "label": "", "kind": "local_business",
        "search_terms": [], "titles": [], "locations": {},
        "description": "", "must_have": [], "good_signals": [],
        "disqualifiers": [], "channel_default": "email", "tone": "",
        "size_hint": "", "interpretation": "",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# The prompt itself keeps discovery and qualification apart
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "phrase",
    [
        "leave search_terms and titles EMPTY",
        "good_signals and disqualifiers are for everything else",
        "never a technology a business uses or lacks",
    ],
)
def test_the_draft_prompt_forbids_inventing_a_category(phrase):
    assert phrase in niches_router.DRAFT_SYSTEM


# --------------------------------------------------------------------------- #
# The backend guard: refuse only when there is truly nothing to go on
# --------------------------------------------------------------------------- #


def test_a_request_with_neither_a_place_nor_a_category_is_refused(monkeypatch):
    """
    "Find good leads" names no place and no category -- there is genuinely
    nothing here for discovery to run on. This is the one case that must
    still be refused, with an actionable message.
    """
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(label="good leads"),
    )
    with pytest.raises(HTTPException) as caught:
        runs_router.parse_request(runs_router.ParseRequest(prompt="find good leads"))
    assert caught.value.status_code == 422
    detail = str(caught.value.detail)
    assert "place" in detail.lower() or "business" in detail.lower()


def test_a_b2b_request_with_neither_a_place_nor_titles_is_also_refused(monkeypatch):
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(label="companies", kind="b2b"),
    )
    with pytest.raises(HTTPException) as caught:
        runs_router.parse_request(runs_router.ParseRequest(prompt="companies that are bad"))
    assert caught.value.status_code == 422


def test_a_place_with_no_category_is_accepted_as_a_broad_search(monkeypatch):
    """
    The actual "Germany" case. A well-behaved model followed the drafting
    rule and left search_terms empty, because "without AI-powered customer
    support" names no real, searchable category -- but Germany is a real
    place. This must be ACCEPTED and planned as a broad search, not refused:
    rejecting a request that names a real place is the overcorrection this
    file's docstring describes.
    """
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(
            label="businesses without AI support",
            locations={"EU": ["Germany"]},
            good_signals=["does not appear to use AI-powered customer support"],
        ),
    )
    plan = runs_router.parse_request(
        runs_router.ParseRequest(
            prompt="Find businesses in Germany without AI-powered customer support"
        )
    )
    assert plan["locations"] == ["Germany"]
    assert plan["signals"]


def test_a_b2b_industry_with_no_titles_is_accepted_as_a_broad_search(monkeypatch):
    """
    "Find SaaS companies in the UK hiring sales people": a real industry
    keyword and a real place, no job title. Company-level discovery, not a
    people search -- and not a reason to refuse the request.
    """
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(
            label="SaaS companies", kind="b2b",
            search_terms=["SaaS"], locations={"UK": ["United Kingdom"]},
            good_signals=["actively hiring for sales roles"],
        ),
    )
    plan = runs_router.parse_request(
        runs_router.ParseRequest(prompt="Find SaaS companies in the UK hiring sales people")
    )
    assert plan["locations"] == ["United Kingdom"]


def test_a_real_category_with_a_qualifying_trait_is_not_refused(monkeypatch):
    """
    "Restaurants that don't take online bookings" -- search_terms carries
    the real category, the trait goes to good_signals. This must proceed.
    """
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(
            label="restaurants", search_terms=["restaurant"],
            good_signals=["no online booking system found on their site"],
        ),
    )
    plan = runs_router.parse_request(
        runs_router.ParseRequest(prompt="restaurants that don't take online bookings")
    )
    assert plan["audience"] == "restaurants"
    assert plan["niche_ids"]


def test_a_named_job_title_satisfies_the_b2b_guard(monkeypatch):
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(label="support leads", kind="b2b", titles=["Head of Support"]),
    )
    plan = runs_router.parse_request(
        runs_router.ParseRequest(prompt="Heads of Support at SaaS companies")
    )
    assert plan["audience"] == "support leads"
