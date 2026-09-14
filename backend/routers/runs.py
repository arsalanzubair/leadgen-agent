"""
runs.py -- the routes behind the Find Leads screen.

    POST /api/runs/parse   read a plain-language request into a plan
    POST /api/runs         start a search
    GET  /api/runs         every search this workspace has run
    GET  /api/runs/{id}    one search, including how far it has got
    GET  /api/leads        the businesses those searches turned up

Parsing and starting are separate calls, and parse writes nothing. The
audience a request describes is saved at the moment the user actually starts
the search, not while the product is still working out what they meant -- a
half-typed sentence should not leave an audience behind in their configuration.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import runs, workspace
from backend.routers.niches import draft_niche, DraftRequest, _slug
from src.reliability import ConfigError

router = APIRouter(prefix="/api", tags=["runs"])


class ParseRequest(BaseModel):
    prompt: str


class StartRequest(BaseModel):
    prompt: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    # The audience the parse step drafted. Saved only if its id is new, so a
    # request naming an audience the workspace already has reuses it.
    niche_draft: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Reading a request
# --------------------------------------------------------------------------- #

@router.post("/runs/parse")
def parse_request(body: ParseRequest) -> dict[str, Any]:
    """
    Turn what somebody typed into a plan, using the user's own AI key.

    The audience comes back as a draft rather than an id: until the user starts
    the search there is nothing to point an id at.
    """
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(422, detail="Tell us who you're looking for.")

    tenant_id = workspace.resolve_tenant_id()
    rules = workspace.read_rules(tenant_id)

    # The audience is read by the same code the Targeting draft uses, so one
    # description produces the same understanding wherever it is typed.
    draft = draft_niche(DraftRequest(description=prompt))

    # A request needs SOMETHING to search on -- a place, or a specific
    # category/role -- but not both. "Find businesses in Germany without
    # AI-powered customer support" names a real place and no category at all;
    # that is a legitimate broad search by location, not an incomplete
    # request, and it must be accepted rather than rejected for missing a
    # business type. What genuinely cannot run is a request with neither: no
    # place to look and nothing to look for names it either, e.g. "find good
    # leads". That is the only case caught here, before anything is saved.
    has_location = bool(draft.get("locations"))
    has_category = bool(draft.get("search_terms")) or bool(draft.get("titles"))
    if not has_location and not has_category:
        raise HTTPException(
            422,
            detail=(
                "Add a place to search -- a city, country or region -- or a "
                "specific type of business, trade, or job title. For example "
                "\"Germany\", \"dental clinics\", or \"Heads of Customer "
                "Support\"."
            ),
        )

    existing = {n["id"] for n in workspace.read_niches(tenant_id)}
    niche_id = draft["id"] or _slug(draft["label"])

    # Regions: the ones the drafted audience placed a location in, else every
    # region this workspace works in.
    located = [r for r in draft.get("locations", {}) if r in rules["regions"]]
    regions = located or list(rules["regions"])

    channel = draft.get("channel_default") or "email"
    channels = ["email", "linkedin"] if channel == "both" else [channel]

    return {
        "offering": "",
        "audience": draft["label"],
        "niche_ids": [niche_id],
        "regions": regions,
        "locations": [
            place
            for region, places in draft.get("locations", {}).items()
            if region in rules["regions"]
            for place in places
        ],
        "signals": draft.get("good_signals", []),
        "channels": channels,
        "language_map": {
            region: rules.get("language_map", {}).get(region, "en") for region in regions
        },
        "fit_score_threshold": rules.get("fit_score_threshold", 60),
        "max_leads_per_run": rules.get("max_leads_per_run", 25),
        "follow_up_pace_days": 3,
        # There is no preview mode: a plan a user starts is a real search,
        # using whatever the workspace has actually connected.
        "dry_run": False,
        # Carried so `POST /api/runs` can save it if the id is new. Not shown.
        "niche_draft": None if niche_id in existing else draft,
        "interpretation": draft.get("interpretation", ""),
    }


# --------------------------------------------------------------------------- #
# Starting one
# --------------------------------------------------------------------------- #

@router.post("/runs")
def start_run(body: StartRequest) -> dict[str, Any]:
    tenant_id = workspace.resolve_tenant_id()
    config = dict(body.config)

    niche_ids = [str(n) for n in config.get("niche_ids", []) if str(n).strip()]
    if not niche_ids:
        raise HTTPException(422, detail="Tell us who you're looking for.")

    # Save the drafted audience if the run needs one that does not exist yet.
    # Without this the graph has nothing to target and the run would fail
    # several seconds later with a message about configuration.
    existing = {n["id"] for n in workspace.read_niches(tenant_id)}
    missing = [n for n in niche_ids if n not in existing]
    if missing:
        draft = body.niche_draft
        if not draft:
            raise HTTPException(
                422,
                detail="That audience is not set up yet. Describe who you want "
                "to find and start the search again.",
            )
        has_location = bool(draft.get("locations"))
        has_category = bool(draft.get("search_terms")) or bool(draft.get("titles"))
        if not has_location and not has_category:
            raise HTTPException(
                422,
                detail="That audience has no place to search and no specific "
                "business type, trade or job title. Describe a place or a "
                "category and start the search again.",
            )
        payload = {k: v for k, v in draft.items() if k != "interpretation"}
        payload["id"] = missing[0]
        try:
            workspace.save_niche(tenant_id, payload, creating=True)
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc))
        config["niche_ids"] = [missing[0]]

    try:
        return runs.start(tenant_id, config, body.prompt)
    except runs.RunBusy as exc:
        # 409: the request was fine, the timing was not.
        raise HTTPException(409, detail=str(exc))


# --------------------------------------------------------------------------- #
# Reading one back
# --------------------------------------------------------------------------- #

@router.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    return runs.list_runs(workspace.resolve_tenant_id())


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    record = runs.get_run(workspace.resolve_tenant_id(), run_id)
    if record is None:
        raise HTTPException(404, detail="That search could not be found.")
    return record


@router.get("/leads")
def list_leads() -> list[dict[str, Any]]:
    return runs.leads_from_runs(workspace.resolve_tenant_id())
