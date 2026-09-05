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
        raise HTTPException(422, detail=str(exc))


@router.put("/{niche_id}")
def update_niche(niche_id: str, payload: NichePayload) -> dict[str, Any]:
    body = payload.model_dump()
    body["id"] = niche_id
    try:
        return workspace.save_niche(workspace.resolve_tenant_id(), body, creating=False)
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc))


@router.delete("/{niche_id}")
def delete_niche(niche_id: str) -> dict[str, str]:
    try:
        workspace.delete_niche(workspace.resolve_tenant_id(), niche_id)
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc))
    return {"id": niche_id}


# --------------------------------------------------------------------------- #
# Drafting one from a description
# --------------------------------------------------------------------------- #

DRAFT_SYSTEM = """You turn a business owner's description of who they want to \
reach into a structured targeting definition.

You work for ANY industry. Never assume a vertical, never substitute a \
different industry for the one described, and never invent a niche the person \
did not ask for. If they say "machine shops", the audience is machine shops.

Rules:
- kind is "local_business" for anything found on a map (shops, clinics, \
trades, restaurants, salons, gyms), and "b2b" for companies reached through a \
named person in a role.
- search_terms: 2-5 short phrases somebody would type into a maps search. \
Only for local_business; leave empty for b2b.
- titles: 3-6 job titles worth writing to. Only for b2b; leave empty otherwise.
- good_signals: 4-6 observable, checkable things that make one of these worth \
contacting. Each must be something you could verify from a website or a job \
posting - not a guess about intent.
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
        f"Places this workspace operates in: {regions}. Use only these region "
        f"codes as keys in `locations`, and put the cities or countries the "
        f"description mentions under the right one. If no place is named, "
        f"leave locations empty.\n\n"
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
    except Exception as exc:  # noqa: BLE001 - any provider failure is the same to the user
        raise HTTPException(
            502,
            detail="The AI connection could not read that. Try describing it "
            f"differently, or build the audience yourself. ({exc.__class__.__name__})",
        )

    kind = parsed.get("kind") if parsed.get("kind") in ("local_business", "b2b") else "local_business"
    channel = parsed.get("channel_default")
    if channel not in ("email", "linkedin", "both"):
        channel = "linkedin" if kind == "b2b" else "email"

    locations = {
        region: [str(place) for place in places if str(place).strip()]
        for region, places in (parsed.get("locations") or {}).items()
        if region in regions and places
    }

    label = str(parsed.get("label") or description)[:80]

    return {
        "id": _slug(label),
        "label": label,
        "kind": kind,
        "search_terms": _strings(parsed.get("search_terms")) if kind == "local_business" else [],
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
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
