"""
niches.py -- the audiences the user wants to find.

An editor for the workspace's own configuration file, not a parallel store:
every write goes through the agent's own validator before it replaces the file,
so the UI cannot produce a configuration that would halt the next run.

`POST /api/niches/draft` turns a description into a structured audience using
the USER'S OWN AI key. It returns a draft for review and saves nothing --
the same confirm-before-commit shape as Find Leads. With no AI key configured
it says so plainly rather than failing silently or returning something
plausible that came from a hard-coded list.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import workspace
from src.integrations import llm
from src.integrations.redact import scrub
from src.reliability import ConfigError

router = APIRouter(prefix="/api/niches", tags=["targeting"])


class NichePayload(BaseModel):
    id: str = ""
    label: str
    kind: str = "local_business"
    search_terms: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    locations: dict[str, list[str]] = Field(default_factory=dict)
    description: str = ""
    must_have: list[str] = Field(default_factory=list)
    good_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    channel_default: str = "email"
    tone: str = ""
    size_hint: str = ""


class DraftRequest(BaseModel):
    description: str


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:48]


#: Common ways a person names one of this workspace's regions without using
#: its code. The drafting prompt asks the model to key `locations` with the
#: code directly, but "the user said USA" often survives into its answer
#: verbatim -- this maps the spelling people actually use to the fixed, small
#: set of codes `config/compliance_profiles.yaml` defines. It is a spelling
#: normalisation, not a guess: unlike a business category, the set of things
#: "USA" can mean is not open-ended.
_REGION_ALIASES: dict[str, str] = {
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US",
    "united states": "US", "united states of america": "US", "america": "US",
    "uk": "UK", "u.k.": "UK", "united kingdom": "UK", "britain": "UK",
    "great britain": "UK", "england": "UK", "scotland": "UK", "wales": "UK",
    "northern ireland": "UK",
    "eu": "EU", "europe": "EU", "european union": "EU",
    "ca": "CA", "canada": "CA",
    "au": "AU", "aus": "AU", "australia": "AU",
    "me": "ME", "middle east": "ME", "uae": "ME", "u.a.e.": "ME",
    "united arab emirates": "ME", "saudi arabia": "ME", "ksa": "ME", "gcc": "ME",
}


def _normalise_region(key: str, regions: list[str]) -> str | None:
    """
    Match a region name the model returned to one this workspace actually
    operates in, tolerating the spelling and case a person would type even
    though the prompt asks for the code itself.

    None means the place named is genuinely not one of this workspace's
    regions -- the caller's job, not this function's, to decide what an
    unsupported region should do.
    """
    text = key.strip().lower()
    if not text:
        return None
    for region in regions:
        if text == region.lower():
            return region
    mapped = _REGION_ALIASES.get(text)
    return mapped if mapped in regions else None


@router.get("")
def list_niches() -> list[dict[str, Any]]:
    return workspace.read_niches(workspace.resolve_tenant_id())


@router.post("")
def create_niche(payload: NichePayload) -> dict[str, Any]:
    body = payload.model_dump()
    body["id"] = body["id"] or _slug(body["label"])
    if not body["id"]:
        raise HTTPException(422, detail="Give this audience a name.")
    try:
        return workspace.save_niche(workspace.resolve_tenant_id(), body, creating=True)
    except ConfigError as exc:
        raise HTTPException(422, detail=workspace.friendly_config_error(exc))


@router.put("/{niche_id}")
def update_niche(niche_id: str, payload: NichePayload) -> dict[str, Any]:
    body = payload.model_dump()
    body["id"] = niche_id
    try:
        return workspace.save_niche(workspace.resolve_tenant_id(), body, creating=False)
    except ConfigError as exc:
        raise HTTPException(422, detail=workspace.friendly_config_error(exc))


@router.delete("/{niche_id}")
def delete_niche(niche_id: str) -> dict[str, str]:
    try:
        workspace.delete_niche(workspace.resolve_tenant_id(), niche_id)
    except ConfigError as exc:
        raise HTTPException(422, detail=workspace.friendly_config_error(exc))
    return {"id": niche_id}


# --------------------------------------------------------------------------- #
# Drafting one from a description
# --------------------------------------------------------------------------- #

DRAFT_SYSTEM = """You turn a business owner's description of who they want to \
reach into a structured targeting definition.

You work for ANY industry. Never assume a vertical, never substitute a \
different industry for the one described, and never invent a niche the person \
did not ask for. If they say "machine shops", the audience is machine shops.

DISCOVERY vs QUALIFICATION -- keep these separate, because they are answered \
by different tools:
- search_terms and titles are what a map search or a contact database can \
actually look up: a type of shop, trade or profession ("dental clinic", \
"machine shop"), a real industry or company keyword ("SaaS", "logistics"), \
or a job title ("Head of Support"). These must be real, searchable \
categories -- never a technology a business uses or lacks, a policy, a \
practice, or anything only knowable from reading their website.
- good_signals and disqualifiers are for everything else the description \
mentions that discovery cannot filter on: using or lacking a particular \
tool, having or lacking some feature, following some practice, hiring for a \
role. "without AI-powered customer support", "still using paper forms", \
"using competitor X", "hiring salespeople" all belong here, to be checked \
once a business is FOUND -- never used to find one.
- When the description names both a real category and a qualifying trait \
("restaurants that don't take online bookings", "SaaS companies hiring \
salespeople"), split them: search_terms gets the category, \
good_signals/disqualifiers gets the trait.
- When the description names NO real, searchable category at all -- only a \
trait, or a word too generic to search with ("businesses", "companies") -- \
leave search_terms and titles EMPTY rather than inventing one. An invented \
category returns confident-looking results for the wrong audience; an \
honest empty list says plainly that this is a broad search by location \
rather than a search for one kind of business. Broad is a valid outcome, \
never a reason to invent a category.

Rules:
- kind is "local_business" for anything found on a map (shops, clinics, \
trades, restaurants, salons, gyms), and "b2b" for companies or people found \
through a company database rather than a map.
- search_terms: 2-5 short phrases. For local_business, phrases somebody would \
type into a maps search. For b2b, real industry or company keywords (never a \
job title) when the description names one -- otherwise empty per the rule \
above.
- titles: 3-6 job titles worth writing to, only when the description names \
specific roles or seniority. Only for b2b; leave empty otherwise, and leave \
empty for a company-level b2b request that names no role at all.
- good_signals: 4-6 observable, checkable things that make one of these worth \
contacting, including any qualifying trait from the description that is not \
itself a search category. Each must be something you could verify from a \
website or a job posting - not a guess about intent.
- disqualifiers: 2-4 things that rule one out.
- must_have: 1-2 things that must be true for the business to be reachable.
- channel_default: "email" for local businesses, "linkedin" for companies \
reached through a role, unless the description says otherwise.
- interpretation: one sentence, addressed to the user, saying what you \
understood. Plain language, no jargon."""


@router.post("/draft")
def draft_niche(body: DraftRequest) -> dict[str, Any]:
    """
    Read a description into a structured audience, using the user's own AI key.
    """
    description = body.description.strip()
    if not description:
        raise HTTPException(422, detail="Describe the kind of business you want to find.")

    # An honest failure. The alternative -- silently producing something from a
    # built-in list -- would look like it worked and target the wrong people.
    if llm.active_provider() == "mock":
        raise HTTPException(
            422,
            detail="This needs an AI connection. Connect one in Settings, "
            "Connections - or build the audience yourself instead.",
        )

    regions = workspace.read_rules(workspace.resolve_tenant_id())["regions"]

    prompt = (
        f"The user says: {description!r}\n\n"
        f"Places this workspace operates in: {regions}. `locations` keys must "
        f"be EXACTLY one of these codes -- never a country name, and never a "
        f"variant like \"USA\" or \"United States\" in place of \"US\" -- with "
        f"the cities or countries the description mentions listed under the "
        f"matching one. If no place is named, leave locations empty.\n\n"
        "Return JSON with keys: label, kind, search_terms, titles, locations, "
        "description, must_have, good_signals, disqualifiers, channel_default, "
        "size_hint, interpretation."
    )

    try:
        parsed, _ = llm.complete_json(
            prompt,
            task="draft_niche",
            system=DRAFT_SYSTEM,
            temperature=0.3,
            required_keys=("label", "kind", "good_signals", "interpretation"),
        )
    except Exception as exc:  # noqa: BLE001 - every provider failure lands here
        # The reason, not just the exception's class name.
        #
        # This used to report "(LLMUnavailable)" and nothing else, which says
        # only that something went wrong somewhere in a chain of providers --
        # not which one, nor why. The underlying message already names the
        # provider, the status and the model, and it has been through
        # `scrub` on the way out of the LLM layer; it is scrubbed again here
        # because a failure from anywhere else has not been.
        reason = scrub(str(exc)).strip() or exc.__class__.__name__
        if len(reason) > 400:
            reason = reason[:399] + "…"
        raise HTTPException(
            502,
            detail=(
                "The AI connection could not read that. Try describing it "
                f"differently, or build the audience yourself. Details: {reason}"
            ),
        )

    kind = parsed.get("kind") if parsed.get("kind") in ("local_business", "b2b") else "local_business"
    channel = parsed.get("channel_default")
    if channel not in ("email", "linkedin", "both"):
        channel = "linkedin" if kind == "b2b" else "email"

    locations: dict[str, list[str]] = {}
    unmatched_locations: list[str] = []
    for region, places in (parsed.get("locations") or {}).items():
        cleaned = [str(place).strip() for place in (places or []) if str(place).strip()]
        if not cleaned:
            continue
        match = _normalise_region(str(region), regions)
        if match:
            locations.setdefault(match, []).extend(cleaned)
        else:
            # A place the model named but that this workspace does not
            # operate in. Dropping it silently would turn "find dental
            # clinics in Japan" into "find dental clinics everywhere this
            # workspace operates" with no indication that Japan was ignored
            # -- exactly the silent substitution this drafter must never do.
            unmatched_locations.extend(cleaned)

    label = str(parsed.get("label") or description)[:80]

    return {
        "id": _slug(label),
        "label": label,
        "kind": kind,
        # search_terms now applies to both kinds: a maps-search phrase for
        # local_business, an organization keyword (industry, product
        # category) for b2b. titles stays b2b-only -- a map search has no
        # concept of a job title.
        "search_terms": _strings(parsed.get("search_terms")),
        "titles": _strings(parsed.get("titles")) if kind == "b2b" else [],
        "locations": locations,
        "description": str(parsed.get("description") or "").strip(),
        "must_have": _strings(parsed.get("must_have")),
        "good_signals": _strings(parsed.get("good_signals")),
        "disqualifiers": _strings(parsed.get("disqualifiers")),
        "channel_default": channel,
        "tone": "",
        "size_hint": str(parsed.get("size_hint") or "").strip(),
        "interpretation": str(parsed.get("interpretation") or "").strip(),
        # Not part of the saved audience -- read by the routes below to tell
        # the user a named place was not searched, instead of quietly
        # searching every region this workspace operates in as though no
        # place had been named at all.
        "unmatched_locations": unmatched_locations,
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
