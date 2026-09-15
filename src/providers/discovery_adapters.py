"""
discovery_adapters.py -- where leads come from, behind one interface.

Four real sources today, split across the two discovery capabilities because
they answer genuinely different questions:

  discovery_local   osm             "dental clinics in Manchester", free,
                                     no key, thinner data
  discovery_local   google_places   the same search against Google Maps /
                                     Google Business Profile listings --
                                     a website, phone, rating and review
                                     count where OpenStreetMap usually has
                                     none, for a workspace that connects a key
  discovery_b2b     apollo          "Heads of Support at SaaS firms", or
                                     "SaaS companies in the UK" with no title
                                     named, or "businesses in Germany" with
                                     neither a title nor a map category
  discovery_b2b     csv_import      "the export I already have on disk"

`ApolloDiscovery` itself picks people-search vs organization-search per
request (job titles present or not) -- see its docstring below. That is the
one place a request with no map category and no job title still reaches a
real search instead of failing: `n1_discovery` routes such a request to this
capability at all, and this adapter decides which of Apollo's two endpoints
answers it.

Each adapter calls the module function it wraps and translates that vendor's
result type into `DiscoveredBusiness`. The translation is the whole job: it is
what lets N1 stop having one code path per vendor, and it is why adding a new
source does not touch N1 at all.

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
from src.providers.results import ErrorCode, ProviderError, ProviderResult
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


def _from_organization(org: apollo.OrganizationResult) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        company_name=org.company_name,
        website=org.website,
        industry=org.industry,
        location=org.location,
        linkedin_url=org.linkedin_url,
        employee_count=org.employee_count,
        source=org.source,
    )


# --------------------------------------------------------------------------- #
# Local businesses
# --------------------------------------------------------------------------- #


def _run_local_search(
    request: DiscoveryRequest,
    *,
    search_fn,
    provider_id: str,
) -> ProviderResult[list[DiscoveredBusiness]]:
    """
    Shared by every local-business adapter: run one (search_term, location)
    pair at a time through whichever vendor function the caller passes, and
    fold the results into `DiscoveredBusiness`. `search_fn` takes `(term,
    location, limit)` -- not a pre-joined query string -- so OpenStreetMap's
    adapter can hand the category to a structured, tag-based search rather
    than always falling back to free text. The only thing that differs
    between OpenStreetMap and Google Maps is which function answers each pair
    and which id gets recorded -- everything else, right down to the wording
    of the "nothing to search on" error, is identical.
    """
    # No category or nowhere to look is not "zero businesses" -- it is
    # nothing having been asked. Looping over an empty list here used to
    # produce a silent, unlogged `[]` indistinguishable from a real search
    # that came up empty.
    if not request.search_terms or not request.locations:
        return ProviderResult.failure(
            ProviderError(
                ErrorCode.CONFIGURATION_ERROR,
                provider=provider_id,
                operation="discover_local",
                message=(
                    f"niche={request.niche_id!r} region={request.region!r}: "
                    f"search_terms={request.search_terms!r} "
                    f"locations={request.locations!r}"
                ),
                user_message="This audience has no searchable category or place to look in.",
                user_action="Describe a specific kind of business and a place to search, "
                "or edit the audience in Settings.",
            ),
            data=[],
        )

    found: list[DiscoveredBusiness] = []
    errors: list[ProviderError] = []
    for term in request.search_terms:
        for location in request.locations:
            log.info(
                "discovery niche=%s region=%s term=%r location=%r provider=%s",
                request.niche_id, request.region, term, location, provider_id,
            )
            result = search_fn(term, location, request.limit)
            if result.ok:
                found.extend(_from_place(p) for p in result.data)
            else:
                errors.append(result.error)  # type: ignore[arg-type]

    if not found and errors:
        # Every query that ran failed outright -- this is not a quiet
        # region, it is a provider that never actually answered.
        return ProviderResult.failure(
            errors[0], data=[], metadata={"failed_queries": len(errors)}
        )
    return ProviderResult.success(
        provider_id, "discover_local", found,
        metadata={"failed_queries": len(errors)} if errors else {},
    )


class LocalSearchDiscovery:
    """
    Local businesses by area, through `integrations.places`'s OpenStreetMap
    path only.

    Needs no credentials: OpenStreetMap's Nominatim is free, and the module it
    wraps already enforces that service's hard one-request-per-second policy
    globally rather than per call. This adapter no longer looks for a Google
    Places key on its own -- Google Maps is its own selectable provider,
    `GoogleMapsDiscovery` below, and a workspace picks it explicitly (with
    `osm` as an optional `fallback`) rather than this one silently upgrading
    itself whenever a key happens to be in the environment.
    """

    id = "osm"
    kind = "local_business"

    def available(self) -> bool:
        return True

    def find(self, request: DiscoveryRequest) -> ProviderResult[list[DiscoveredBusiness]]:
        return _run_local_search(
            request, search_fn=places.search_local_structured, provider_id="osm"
        )


class GoogleMapsDiscovery:
    """
    Local businesses from Google Maps / Google Business Profile listings, via
    `integrations.places`'s Google Places (New) Text Search -- a verified
    website, phone number, rating and review count where OpenStreetMap
    usually has none.

    Uses `places.search()`, which prefers Google and degrades to OpenStreetMap
    on a Google-side failure (quota spent, a rejected key discovered mid-batch,
    an outage) rather than stopping discovery outright; the per-row `source`
    still says which one actually answered, so choosing Google Maps never
    silently mislabels an OSM result as Google's. `available()` reports
    whether a key is configured at all, which is what lets a workspace name
    `osm` as this provider's `fallback` and have the resolver switch to it
    automatically when no key has ever been connected -- a different case from
    a connected key failing mid-search, which `places.search()` handles
    itself.
    """

    id = "google_places"
    kind = "local_business"

    #: Unlike Overpass (which needs a specific tag to query), Google Places'
    #: free-text search has no trouble with a plain, location-only query --
    #: `discover_for_target` reads this to decide whether a categoryless
    #: local_business request ("find businesses in Germany") can stay local
    #: instead of falling back to a company database.
    supports_categoryless_search = True

    def available(self) -> bool:
        return places.has_google_key()

    def find(self, request: DiscoveryRequest) -> ProviderResult[list[DiscoveredBusiness]]:
        def search_fn(term: str, location: str, limit: int) -> ProviderResult:
            return places.search(places.build_query(term, location), limit=limit)

        return _run_local_search(request, search_fn=search_fn, provider_id="google_places")


# --------------------------------------------------------------------------- #
# Business contacts
# --------------------------------------------------------------------------- #


class ApolloDiscovery:
    """
    People by job title, or companies by location/industry, through
    `integrations.apollo` -- routed by what the request actually names rather
    than by the niche's `type`.

    A request with job titles is a people search: somebody wants a named
    contact in a specific role. A request with none -- "SaaS companies in the
    UK", or nothing more specific than a location at all, "businesses in
    Germany" -- names no role to look a contact up by, so it is a
    company-level search instead. Forcing the second case through the people
    endpoint with `person_titles` empty would ask a contact database a
    question it cannot answer; Apollo's own organization search is the
    correct tool, and this is where that choice is made, per request, not
    per niche.

    Both `apollo.search()` and `apollo.search_organizations()` are additive
    rather than exclusive: when the API returns fewer than asked for and a
    CSV glob is set, they top up from the import folder. That behaviour is
    kept -- an empty API result with usable exports sitting on disk should
    still produce a batch -- which does mean this adapter can return rows
    sourced from CSV. The per-row `source` says so.
    """

    id = "apollo"
    kind = "b2b"

    def available(self) -> bool:
        """
        Always true, and not an oversight.

        Both `apollo.search()` and `apollo.search_organizations()` degrade to
        the CSV import folder on their own when there is no API key -- that is
        how a keyless install has always produced B2B leads. Reporting
        unavailable here would make the resolver skip to the null adapter and
        throw those rows away. When there is neither a key nor a usable
        export, `find()` returns nothing and the wrapped module logs which of
        the two was missing.
        """
        return True

    def find(self, request: DiscoveryRequest) -> ProviderResult[list[DiscoveredBusiness]]:
        if not request.titles and not request.locations and not request.search_terms and not request.industries:
            # Nothing to search on at all -- not even a place. An unfiltered
            # organization search would spend a real Apollo credit (and a CSV
            # fallback would return every company on disk) to answer a
            # question nobody asked. Same rule as LocalSearchDiscovery: no
            # category or nowhere to look is not "zero businesses", it is
            # nothing having been asked.
            return ProviderResult.failure(
                ProviderError(
                    ErrorCode.CONFIGURATION_ERROR,
                    provider="apollo",
                    operation="discover",
                    message=f"niche={request.niche_id!r} region={request.region!r}: "
                    "no titles, locations, search terms or industries given",
                    user_message="This audience has no place to search and no specific "
                    "role or company keyword.",
                    user_action="Describe a place to search, or a job title, trade or "
                    "industry, and try again.",
                ),
                data=[],
            )

        if request.titles:
            result = apollo.search(
                titles=request.titles,
                locations=request.locations,
                industries=request.industries,
                employee_range=request.employee_range,
                limit=request.limit,
                csv_glob=request.csv_glob,
            )
            if not result.ok:
                return ProviderResult.failure(result.error, data=[], metadata=result.metadata)  # type: ignore[arg-type]
            return ProviderResult.success(
                "apollo", "discover_contacts",
                [_from_contact(contact) for contact in result.data],
                metadata=result.metadata,
            )

        result = apollo.search_organizations(
            keywords=request.search_terms,
            locations=request.locations,
            industries=request.industries,
            employee_range=request.employee_range,
            limit=request.limit,
            csv_glob=request.csv_glob,
        )
        if not result.ok:
            return ProviderResult.failure(result.error, data=[], metadata=result.metadata)  # type: ignore[arg-type]
        return ProviderResult.success(
            "apollo", "discover_companies",
            [_from_organization(org) for org in result.data],
            metadata=result.metadata,
        )


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

    def find(self, request: DiscoveryRequest) -> ProviderResult[list[DiscoveredBusiness]]:
        pattern = request.csv_glob or "*.csv"
        contacts = apollo.import_csvs(pattern)
        if not contacts:
            log.info(
                "no CSV rows matched %r in %s for niche=%s",
                pattern, apollo.csv_import_dir(), request.niche_id,
            )
        limit = request.limit
        rows = [_from_contact(contact) for contact in contacts]
        return ProviderResult.success(
            "csv_import", "discover_contacts", rows[:limit] if limit else rows,
        )


# --------------------------------------------------------------------------- #
# Null adapter
# --------------------------------------------------------------------------- #


class NoDiscovery:
    """
    What a niche gets when its capability has no usable provider at all.

    This is a configuration fact, not a search that came up empty -- N1 must
    not report it the same way it would report a real provider searching and
    finding nothing, or a genuinely unreachable region looks identical to a
    tenant who never finished setting discovery up.
    """

    def __init__(self, kind: str = "local_business", reason: str = "") -> None:
        self.id = "none"
        self.kind = kind
        self.reason = reason

    def available(self) -> bool:
        return False

    def find(self, request: DiscoveryRequest) -> ProviderResult[list[DiscoveredBusiness]]:
        reason = self.reason or "not configured"
        log.warning(
            "no discovery provider for niche=%s (%s); nothing to search with",
            request.niche_id, reason,
        )
        return ProviderResult.failure(
            ProviderError(
                ErrorCode.CONFIGURATION_ERROR,
                provider="none",
                operation="discover",
                message=f"no usable {self.kind} discovery provider ({reason})",
                user_message="No connected provider can search for this kind of audience yet.",
                user_action="Connect a discovery provider in Settings for this audience type.",
            ),
            data=[],
        )


_ADAPTERS = {
    "osm": LocalSearchDiscovery,
    "google_places": GoogleMapsDiscovery,
    "apollo": ApolloDiscovery,
    "csv_import": CsvImportDiscovery,
}


def build(spec: ProviderSpec, ctx: ProviderContext):
    """Registry factory."""
    adapter = _ADAPTERS.get(spec.id)
    if adapter is None:
        return NoDiscovery(reason=f"{spec.id} has no adapter")
    return adapter()
