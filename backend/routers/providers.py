"""
providers.py -- what can do each job, and what this workspace picked.

  GET  /api/providers                     every provider, grouped by capability
  GET  /api/providers?capability=llm      just the ones that can write
  GET  /api/providers/selection           what this workspace has chosen
  PUT  /api/providers/selection/{cap}     choose one

The list is served from `src/providers/registry.py`, the same list the agent
resolves adapters from. Nothing here is hardcoded and nothing here duplicates
the frontend: the dashboard renders whatever these endpoints declare, which is
why adding a provider is one edit to the registry and no edit to the UI.

No credential appears in any response. The custom-endpoint block that comes
back carries the base URL, the auth style and the headers -- never the token.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from backend.providers import PROVIDERS, PROVIDERS_BY_ID
from backend.workspace import read_selection, resolve_tenant_id, save_selection
from src.providers import registry
from src.providers.base import Capability
from src.reliability import ConfigError

router = APIRouter(prefix="/api/providers", tags=["providers"])


#: Capability -> what the user is actually choosing, in their words. This is
#: the only place these headings live; the dashboard renders them rather than
#: keeping its own copy, so a wording change is a one-line edit here.
CAPABILITY_COPY: dict[str, dict[str, str]] = {
    "llm": {
        "title": "Something to write with",
        "hint": (
            "Reads your requests, scores each business, and writes the "
            "outreach. Nothing works without one of these."
        ),
    },
    "discovery_local": {
        "title": "Finding local businesses",
        "hint": "Shops, clinics, trades, restaurants - anywhere with an address.",
    },
    "discovery_b2b": {
        "title": "Finding people at companies",
        "hint": "By job title and company size, rather than by location.",
    },
    "enrichment": {
        "title": "Finding the right person",
        "hint": (
            "Turns a company into a named contact with a real email address. "
            "Optional, but it lifts the hit rate a lot."
        ),
    },
    "email_sender": {
        "title": "Sending the email",
        "hint": (
            "Your own mailbox or sending service. Without one, everything is "
            "still written and held for your approval."
        ),
    },
    "email_reader": {
        "title": "Watching for replies",
        "hint": "So a reply stops the follow-ups automatically.",
    },
    "crm": {
        "title": "Keeping your records",
        "hint": "Writes every business and outcome somewhere you own.",
    },
    "translation": {
        "title": "Writing in other languages",
        "hint": "Only needed if you are reaching people who do not read English.",
    },
}


class SelectionBody(BaseModel):
    """A provider choice for one capability."""

    primary: str = ""
    fallback: str = ""
    #: Non-secret configuration for a custom endpoint. A `token` key here is
    #: refused by `save_selection` -- keys go through the connection, encrypted.
    settings: dict[str, Any] = Field(default_factory=dict)


def _capability_payload(capability: Capability) -> dict[str, Any]:
    copy = CAPABILITY_COPY.get(capability.value, {})
    specs = registry.for_capability(capability)
    return {
        "capability": capability.value,
        "interface": capability.interface,
        "title": copy.get("title", capability.value),
        "hint": copy.get("hint", ""),
        "default": registry.default_for(capability),
        "providers": [
            PROVIDERS_BY_ID[spec.id].to_dict()
            for spec in specs
            if spec.id in PROVIDERS_BY_ID
        ],
    }


@router.get("")
def list_providers(
    capability: str | None = Query(
        default=None,
        description="Limit to one capability, e.g. llm or discovery_local.",
    ),
) -> list[dict[str, Any]]:
    """
    Every provider that can do each job, grouped by job.

    Includes the ones marked "coming soon" (`enabled: false`) so a dropdown can
    show what is planned without pretending it works. It excludes the internal
    adapters -- the dry-run sender, the null reply reader, the built-in test
    model -- because those are resolvable by the pipeline and have nothing for
    a user to connect.
    """
    if capability is not None:
        try:
            wanted = Capability(capability)
        except ValueError:
            raise HTTPException(
                404,
                detail=(
                    f"'{capability}' is not something this product needs done. "
                    f"Expected one of {sorted(c.value for c in Capability)}."
                ),
            ) from None
        return [_capability_payload(wanted)]

    # Registry order, so the seeded default comes first in each group.
    seen: list[Capability] = []
    for provider in PROVIDERS:
        cap = Capability(provider.category)
        if cap not in seen:
            seen.append(cap)
    return [_capability_payload(cap) for cap in seen]


@router.get("/selection")
def get_selection() -> dict[str, dict[str, Any]]:
    """
    What this workspace has chosen, and whether each choice can actually run.

    `chosen_by` says where the answer came from -- the workspace's own
    configuration, a legacy field, a `.env` variable, or the registry default
    -- so somebody debugging "why is it using Groq" gets an answer instead of
    a guess.
    """
    tenant_id = resolve_tenant_id()
    try:
        return read_selection(tenant_id)
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.put("/selection/{capability}")
def put_selection(capability: str, body: SelectionBody) -> dict[str, dict[str, Any]]:
    """
    Point one capability at a provider.

    Writes the tenant's YAML through the same validated, atomic path every
    other settings write uses, and returns the whole selection afterwards so
    the screen reflects what is actually on disk rather than what it hoped for.
    """
    tenant_id = resolve_tenant_id()
    try:
        return save_selection(
            tenant_id,
            capability,
            primary=body.primary.strip(),
            fallback=body.fallback.strip(),
            settings=body.settings,
        )
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc)) from None
