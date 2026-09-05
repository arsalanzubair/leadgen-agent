"""
enrichment_adapters.py -- turning a company into a named person.

Enrichment in this pipeline is two different things and only one of them is a
provider:

  * Reading the business's own website. Always runs, needs no account, has
    nothing to swap. It stays in `integrations.scraping`, called by N2
    directly, and is not modelled here.
  * Paying a lookup service for an address the site did not publish. That is
    metered, that is a credential, and that is what swaps.

So `website_only` is a real, selectable choice that finds no addresses -- not a
degraded state. A workspace that picks it still gets everything scraping
found; it just never spends a lookup. Modelling that as a provider rather than
as "Hunter is disconnected" is what lets Connections show it as a deliberate
setting instead of a warning.
"""

from __future__ import annotations

from src.integrations import anymail_finder, hunter
from src.providers.base import FoundEmail, ProviderContext
from src.providers.registry import ProviderSpec

#: What an unmetered provider reports as remaining, so N2's budget arithmetic
#: (`min(top_n, remaining)`) never mistakes "no limit" for "none left".
UNMETERED = 1_000_000


class HunterEnrichment:
    """
    Hunter's domain search and email finder.

    Wraps `hunter.find_email`, which owns the part that matters: it spends
    exactly one lookup from the durable monthly counter per call actually made,
    counts a failed call too (a failure may still have been billed), and
    discards a result below the confidence floor rather than sending to an
    address it does not trust.
    """

    id = "hunter"

    def available(self) -> bool:
        return hunter.has_api_key()

    def find_email(
        self, domain: str, *, first_name: str = "", last_name: str = ""
    ) -> FoundEmail | None:
        result = hunter.find_email(
            domain, first_name=first_name, last_name=last_name
        )
        if result is None:
            return None
        return FoundEmail(
            email=result.email,
            confidence=result.confidence,
            first_name=result.first_name,
            last_name=result.last_name,
            position=result.position,
            source=result.source,
        )

    def budget_remaining(self) -> int:
        return hunter.remaining_lookups()

    def budget_status(self) -> str:
        return hunter.quota_status()


class AnymailFinderEnrichment:
    """
    Anymail Finder, behind the same interface as Hunter.

    Metered rather than free, so the counter matters more here than it does
    anywhere else in this system: `anymail_finder.monthly_cap()` defaults to a
    deliberately low 100 lookups a month, and raising it is something a person
    has to decide to do.

    Everything that makes this safe lives in the integration -- one lookup per
    call, counted before the call, and an address the service rates as risky
    thrown away rather than sent to. This class is the interface, and nothing
    else.
    """

    id = "anymail_finder"

    def available(self) -> bool:
        return anymail_finder.has_api_key()

    def find_email(
        self, domain: str, *, first_name: str = "", last_name: str = ""
    ) -> FoundEmail | None:
        result = anymail_finder.find_email(
            domain, first_name=first_name, last_name=last_name
        )
        if result is None:
            return None
        return FoundEmail(
            email=result.email,
            confidence=result.confidence,
            first_name=result.first_name,
            last_name=result.last_name,
            position=result.position,
            source=result.source,
        )

    def budget_remaining(self) -> int:
        return anymail_finder.remaining_lookups()

    def budget_status(self) -> str:
        return anymail_finder.quota_status()


class WebsiteOnlyEnrichment:
    """
    Finds no addresses, and says so in the batch log rather than in a warning.

    Also the null adapter: a workspace with nothing connected for enrichment
    gets this, which is exactly right -- scraping still runs, and no lookup is
    attempted against a service there is no key for.
    """

    id = "website_only"

    def available(self) -> bool:
        return True

    def find_email(
        self, domain: str, *, first_name: str = "", last_name: str = ""
    ) -> FoundEmail | None:
        return None

    def budget_remaining(self) -> int:
        # Zero, not UNMETERED: N2 uses this to decide how many leads to
        # allocate a lookup to, and allocating any would be pointless work.
        return 0

    def budget_status(self) -> str:
        return "reading their website only; no lookup service connected"


_ADAPTERS = {
    "hunter": HunterEnrichment,
    "anymail_finder": AnymailFinderEnrichment,
    "website_only": WebsiteOnlyEnrichment,
}


def build(spec: ProviderSpec, ctx: ProviderContext):
    """Registry factory."""
    adapter = _ADAPTERS.get(spec.id, WebsiteOnlyEnrichment)
    return adapter()
