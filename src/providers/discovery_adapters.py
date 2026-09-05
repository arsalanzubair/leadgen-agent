"""
discovery_adapters.py -- where leads come from, behind one interface.

Three real sources today, split across the two discovery capabilities because
they answer genuinely different questions:

  discovery_local   osm          "dental clinics in Manchester"
  discovery_b2b     apollo       "Heads of Support at 20-300 person SaaS firms"
  discovery_b2b     csv_import   "the export I already have on disk"

Each adapter calls the module function it wraps and translates that vendor's
result type into `DiscoveredBusiness`. The translation is the whole job: it is
what lets N1 stop having one code path per vendor, and it is why adding a
fourth source does not touch N1 at all.

The `source` string that each vendor sets on its own results is carried
through unchanged. It ends up on the lead and in the CRM row, and it is the
only audit trail for "where did this business come from" -- rewriting it to
the adapter's id would lose the distinction between an Apollo API hit and a
row from a hand-built CSV.
"""

from __future__ import annotations

from src.integrations import apollo, places
from src.providers.base import (
    DiscoveredBusiness,
    DiscoveryRequest,
    ProviderContext,
)
from src.providers.registry import ProviderSpec
from src.reliability import log

# --------------------------------------------------------------------------- #
# Vendor result -> DiscoveredBusiness
# --------------------------------------------------------------------------- #


def _from_place(place: places.PlaceResult) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        company_name=place.name,
        website=place.website,
        address=place.address,
        phone=place.phone,
        category=place.category,
        rating=place.rating,
        review_count=place.review_count,
        business_status=place.business_status,
        source=place.source,
    )


def _from_contact(contact: apollo.ContactResult) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        company_name=contact.company_name,
        website=contact.website,
        contact_name=contact.contact_name,
        contact_email=contact.email,
        title=contact.title,
        linkedin_url=contact.linkedin_url,
        location=contact.location,
        industry=contact.industry,
        employee_count=contact.employee_count,
        source=contact.source,
    )


# --------------------------------------------------------------------------- #
# Local businesses
# --------------------------------------------------------------------------- #


class LocalSearchDiscovery:
    """
    Local businesses by area, through `integrations.places`.

    Needs no credentials: OpenStreetMap's Nominatim is free, and the module it
    wraps already enforces that service's hard one-request-per-second policy
    globally rather than per call.

    One honest wrinkle. `places.search()` retains a legacy path that prefers
    Google Places when a `GOOGLE_PLACES_API_KEY` happens to be in the
    environment. Google Places is deliberately NOT in the registry -- it cannot
    be connected through Connections and nothing in the product offers it -- so
    the only way that path runs is if somebody set that variable in `.env`
    themselves. It is left in place because removing it would break those
    installs for no gain, and the source recorded on the lead says which one
    actually answered.
    """

    id = "osm"
    kind = "local_business"

    def available(self) -> bool:
        return True

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        found: list[DiscoveredBusiness] = []
        for term in request.search_terms:
            for location in request.locations:
                query = places.build_query(term, location)
                log.info(
                    "discovery niche=%s region=%s query=%r",
                    request.niche_id, request.region, query,
                )
                for place in places.search(query, limit=request.limit):
                    found.append(_from_place(place))
        return found


# --------------------------------------------------------------------------- #
# Business contacts
# --------------------------------------------------------------------------- #


class ApolloDiscovery:
    """
    People by job title, through `integrations.apollo`.

    `apollo.search()` is additive rather than exclusive: when the API returns
    fewer than asked for and a CSV glob is set, it tops up from the import
    folder. That behaviour is kept -- an empty API result with usable exports
    sitting on disk should still produce a batch -- which does mean this
    adapter can return rows sourced from CSV. The per-row `source` says so.
    """

    id = "apollo"
    kind = "b2b"

    def available(self) -> bool:
        """
        Always true, and not an oversight.

        `apollo.search()` degrades to the CSV import folder on its own when
        there is no API key -- that is how a keyless install has always
        produced B2B leads. Reporting unavailable here would make the resolver
        skip to the null adapter and throw those rows away. When there is
        neither a key nor a usable export, `find()` returns nothing and the
        wrapped module logs which of the two was missing.
        """
        return True

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        contacts = apollo.search(
            titles=request.titles,
            locations=request.locations,
            industries=request.industries,
            employee_range=request.employee_range,
            limit=request.limit,
            csv_glob=request.csv_glob,
        )
        return [_from_contact(contact) for contact in contacts]


class CsvImportDiscovery:
    """
    Contacts from CSV exports on disk, and nothing else. No API, no key, no cap.

    This is the provider that proves the layer works: switching a workspace's
    B2B discovery to `csv_import` has to stop Apollo being called at all, not
    merely change the order things are tried in. So it calls `import_csvs`
    directly and never touches `apollo.search`.
    """

    id = "csv_import"
    kind = "b2b"

    def available(self) -> bool:
        """
        Always true. A missing import folder is a message, not unavailability.

        Falling through to the null adapter here would tell the operator
        "nothing connected for this job", when what they need to hear is which
        folder was searched and which pattern found no files. `find()` logs
        exactly that.
        """
        return True

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        pattern = request.csv_glob or "*.csv"
        contacts = apollo.import_csvs(pattern)
        if not contacts:
            log.info(
                "no CSV rows matched %r in %s for niche=%s",
                pattern, apollo.csv_import_dir(), request.niche_id,
            )
        limit = request.limit
        rows = [_from_contact(contact) for contact in contacts]
        return rows[:limit] if limit else rows


# --------------------------------------------------------------------------- #
# Null adapter
# --------------------------------------------------------------------------- #


class NoDiscovery:
    """
    Finds nothing, on purpose.

    What a niche gets when its capability has no usable provider. N1 reports
    the empty result as a low-yield target, which a human sees on the batch
    summary -- far better than a stack trace, and far better than a silently
    successful batch that touched nobody.
    """

    def __init__(self, kind: str = "local_business", reason: str = "") -> None:
        self.id = "none"
        self.kind = kind
        self.reason = reason

    def available(self) -> bool:
        return False

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        log.warning(
            "no discovery provider for niche=%s (%s); found nothing",
            request.niche_id, self.reason or "not configured",
        )
        return []


_ADAPTERS = {
    "osm": LocalSearchDiscovery,
    "apollo": ApolloDiscovery,
    "csv_import": CsvImportDiscovery,
}


def build(spec: ProviderSpec, ctx: ProviderContext):
    """Registry factory."""
    adapter = _ADAPTERS.get(spec.id)
    if adapter is None:
        return NoDiscovery(reason=f"{spec.id} has no adapter")
    return adapter()
