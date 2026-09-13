"""
The "Germany" regression: discovery criteria vs qualification criteria.

"Find businesses in Germany without AI-powered customer support" has a real
place (Germany) and a real qualifying trait (no AI-powered customer support),
but no real, searchable business category -- "businesses" is not a map
category or a job title. Before this fix, the drafting prompt had no rule
against inventing one, `n1_discovery`'s local-search adapter looped over
whatever category and location it was given with no check that either was
non-empty, and the result was a search that silently found zero businesses,
indistinguishable on screen from a provider failure or a thin region.

The fix has two parts, tested separately:

  * `backend/routers/niches.py::DRAFT_SYSTEM` now tells the model to leave
    `search_terms`/`titles` empty rather than invent a category when the
    description names no real one -- this file cannot call a real model, so
    it is verified by parametrized keyword assertions on the prompt text.
  * `backend/routers/runs.py::parse_request` now refuses to plan a run for a
    niche with nothing discoverable, with an actionable message, rather than
    silently letting it through to a run that always finds nothing --
    verified here directly, since it needs no real model to test.
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
# The backend guard: refuse rather than silently produce a 0-result plan
# --------------------------------------------------------------------------- #


def test_a_request_with_no_discoverable_category_is_refused(monkeypatch):
    """
    A well-behaved model followed the new rule and left search_terms empty,
    because "without AI-powered customer support" names no real, searchable
    category. The backend must say so plainly, not start a run that would
    always find zero businesses.
    """
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(label="businesses without AI support"),
    )
    with pytest.raises(HTTPException) as caught:
        runs_router.parse_request(
            runs_router.ParseRequest(
                prompt="Find businesses in Germany without AI-powered customer support"
            )
        )
    assert caught.value.status_code == 422
    detail = str(caught.value.detail)
    assert "business" in detail.lower() or "job title" in detail.lower()


def test_a_b2b_request_with_no_titles_is_also_refused(monkeypatch):
    monkeypatch.setattr(
        runs_router, "draft_niche",
        lambda body: _draft(label="companies", kind="b2b"),
    )
    with pytest.raises(HTTPException) as caught:
        runs_router.parse_request(runs_router.ParseRequest(prompt="companies that are bad"))
    assert caught.value.status_code == 422


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
