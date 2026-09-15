"""
End-to-end verification of the two exact queries the natural-language search
architecture must handle, run through the real pipeline: a simulated Gemini
response representative of what `DRAFT_SYSTEM` actually asks for, through the
real `draft_niche` normalisation, through the real `parse_request` plan
builder. Nothing here mocks `draft_niche` itself -- that is what
`test_discovery_qualification_split.py` does; this file exercises the layer
underneath it too, so a regression in the LLM-output-to-plan path is caught
even when both layers are individually correct in isolation.

    1. "Find dental clinics in USA that maybe need voice agents"
    2. "Find businesses in Germany without AI-powered customer support"

Both were reported as broken: (1) failed with a raw config-validator message
because "USA" did not match the region code "US"; (2) is the discovery vs.
qualification separation case -- a negative, semantic condition must become a
qualification signal, never a discovery filter, and the search must still run
on Google Places/OSM/a company database rather than search literally for
"businesses without AI-powered customer support".
"""

from __future__ import annotations

from backend.routers import niches as niches_router
from backend.routers import runs as runs_router

REGIONS = ["US", "UK", "EU", "CA", "AU", "ME"]


def _mock_llm(monkeypatch, parsed: dict):
    monkeypatch.setattr(niches_router.llm, "active_provider", lambda: "gemini")
    monkeypatch.setattr(niches_router.llm, "complete_json", lambda *a, **k: (parsed, None))
    monkeypatch.setattr(
        niches_router.workspace, "read_rules", lambda tenant_id: {"regions": REGIONS}
    )
    monkeypatch.setattr(niches_router.workspace, "resolve_tenant_id", lambda: "t1")
    monkeypatch.setattr(runs_router.workspace, "read_rules", lambda tenant_id: {"regions": REGIONS})
    monkeypatch.setattr(runs_router.workspace, "resolve_tenant_id", lambda: "t1")
    monkeypatch.setattr(runs_router.workspace, "read_niches", lambda tenant_id: [])


def test_voice_agent_query_end_to_end(monkeypatch):
    """"Find dental clinics in USA that maybe need voice agents"."""
    _mock_llm(monkeypatch, {
        "label": "dental clinics in the USA - voice agent prospects",
        "kind": "local_business",
        "search_terms": ["dental clinic", "dental practice"],
        "titles": [],
        "locations": {"USA": ["United States"]},
        "description": "Dental clinics that may benefit from an AI voice agent.",
        "must_have": ["has a public phone number"],
        "good_signals": [
            "no online booking system found on their site",
            "phone number prominently displayed",
            "front-desk dependence for scheduling appointments",
        ],
        "disqualifiers": ["already advertises an AI voice agent or virtual receptionist"],
        "channel_default": "email",
        "size_hint": "1-50 employees",
        "interpretation": (
            "Looking for dental clinics in the United States that may need an "
            "AI voice agent."
        ),
    })

    draft = niches_router.draft_niche(
        niches_router.DraftRequest(description="Find dental clinics in USA that maybe need voice agents")
    )

    # United States is extracted and normalised to this workspace's region
    # code, not lost for being spelled "USA".
    assert draft["locations"] == {"US": ["United States"]}
    assert draft["unmatched_locations"] == []
    # Dental clinics is the discovery category -- a real, searchable term.
    assert draft["search_terms"] == ["dental clinic", "dental practice"]
    # The voice-agent opportunity became qualification signals, not a
    # discovery term.
    assert not any("voice agent" in term.lower() for term in draft["search_terms"])
    assert any("voice agent" in s.lower() or "booking" in s.lower() or "front-desk" in s.lower()
               for s in draft["good_signals"])

    plan = runs_router.parse_request(
        runs_router.ParseRequest(
            prompt="Find dental clinics in USA that maybe need voice agents"
        )
    )
    assert plan["locations"] == ["United States"]
    assert plan["regions"] == ["US"]
    assert plan["niche_ids"]
    assert plan["signals"]
    assert plan["interpretation"]


def test_ai_customer_support_query_end_to_end(monkeypatch):
    """"Find businesses in Germany without AI-powered customer support"."""
    _mock_llm(monkeypatch, {
        "label": "businesses in Germany without AI customer support",
        "kind": "local_business",
        "search_terms": [],
        "titles": [],
        "locations": {"EU": ["Germany"]},
        "description": "Businesses in Germany that likely lack AI-powered customer support.",
        "must_have": [],
        "good_signals": [
            "no chatbot or AI assistant detected on the site",
            "customer support directed to phone or a static contact form only",
        ],
        "disqualifiers": ["site already advertises an AI chatbot or virtual assistant"],
        "channel_default": "email",
        "size_hint": "",
        "interpretation": (
            "Looking for businesses in Germany that don't appear to use "
            "AI-powered customer support."
        ),
    })

    draft = niches_router.draft_niche(
        niches_router.DraftRequest(
            description="Find businesses in Germany without AI-powered customer support"
        )
    )

    # Germany is a real place named in the EU region grouping this workspace
    # actually uses -- accepted even though the model returned the country
    # name itself as the key, not the region code.
    assert draft["locations"] == {"EU": ["Germany"]}
    # "Businesses" names no real, searchable category -- search_terms must
    # stay empty rather than have the negative condition folded into it.
    assert draft["search_terms"] == []
    assert not any("customer support" in term.lower() for term in draft["search_terms"])
    # The qualifying condition lives in good_signals, ready to be checked once
    # a business is found -- never used to find one.
    assert any("ai" in s.lower() or "chatbot" in s.lower() for s in draft["good_signals"])

    plan = runs_router.parse_request(
        runs_router.ParseRequest(
            prompt="Find businesses in Germany without AI-powered customer support"
        )
    )
    # A place with no category is a legitimate broad search -- accepted, not
    # refused for missing a business type.
    assert plan["locations"] == ["Germany"]
    assert plan["regions"] == ["EU"]
    assert plan["niche_ids"]
    assert plan["signals"]
