"""
places.py -- local business discovery.

Google Places API (New) Text Search when GOOGLE_PLACES_API_KEY is set, guarded
by a durable monthly request counter so the free credit cannot be silently
blown through. OpenStreetMap / Nominatim is the no-key fallback and honours
their usage policy: one request per second, descriptive User-Agent.

Both return the same `PlaceResult` shape, so N1 does not branch on provider.

Env: GOOGLE_PLACES_API_KEY, GOOGLE_PLACES_MONTHLY_REQUEST_CAP,
     NOMINATIM_USER_AGENT
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import requests

from src.counters import QuotaExceeded, places_counter
from src.providers.results import (
    ErrorCode,
    ProviderError,
    ProviderResult,
    call_with_retries,
    classify_exception,
    classify_http_status,
)
from src.reliability import log
from src.settings import env

GOOGLE_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

#: Only the fields we actually use -- Places bills by field mask, so asking for
#: less directly extends the free credit.
GOOGLE_FIELD_MASK = ",".join((
    "places.displayName",
    "places.formattedAddress",
    "places.websiteUri",
    "places.nationalPhoneNumber",
    "places.primaryTypeDisplayName",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
))


@dataclass
class PlaceResult:
    """One discovered local business, provider-agnostic."""

    name: str
    address: str = ""
    website: str = ""
    phone: str = ""
    category: str = ""
    rating: float | None = None
    review_count: int | None = None
    business_status: str = ""
    source: str = "google_places"

    @property
    def is_open(self) -> bool:
        return self.business_status.upper() not in ("CLOSED_PERMANENTLY",)


# --------------------------------------------------------------------------- #
# Nominatim politeness: their policy is a hard 1 req/sec, globally.
# --------------------------------------------------------------------------- #

_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


def _nominatim_throttle() -> None:
    global _nominatim_last_call
    with _nominatim_lock:
        elapsed = time.monotonic() - _nominatim_last_call
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        _nominatim_last_call = time.monotonic()


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

def has_google_key() -> bool:
    return bool(env("GOOGLE_PLACES_API_KEY"))


_GOOGLE_OP = "discover_local"
_NOMINATIM_OP = "discover_local"


def _search_google(query: str, limit: int) -> list[PlaceResult]:
    """Raises `ProviderError` for every failure -- `[]` here would mean Places
    answered and found nothing, never that the call did not complete."""
    counter = places_counter()
    counter.check(1)          # raises QuotaExceeded before spending the credit

    try:
        response = requests.post(
            GOOGLE_TEXT_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": env("GOOGLE_PLACES_API_KEY"),
                "X-Goog-FieldMask": GOOGLE_FIELD_MASK,
            },
            json={"textQuery": query, "maxResultCount": min(limit, 20)},
            timeout=30,
        )
    except requests.exceptions.Timeout as exc:
        raise classify_exception(exc, provider="google_places", operation=_GOOGLE_OP) from exc
    except requests.exceptions.RequestException as exc:
        raise classify_exception(exc, provider="google_places", operation=_GOOGLE_OP) from exc

    counter.consume(1)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise classify_http_status(
            429, response.text, provider="google_places", operation=_GOOGLE_OP,
            retry_after=float(retry_after) if retry_after else None,
        )
    if response.status_code != 200:
        raise classify_http_status(
            response.status_code, response.text, provider="google_places", operation=_GOOGLE_OP,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            ErrorCode.BAD_RESPONSE, provider="google_places", operation=_GOOGLE_OP,
            message=f"could not parse response as JSON: {exc}",
        ) from exc

    results: list[PlaceResult] = []
    for place in payload.get("places", []):
        results.append(
            PlaceResult(
                name=(place.get("displayName") or {}).get("text", "").strip(),
                address=place.get("formattedAddress", ""),
                website=place.get("websiteUri", ""),
                phone=place.get("nationalPhoneNumber", ""),
                category=(place.get("primaryTypeDisplayName") or {}).get("text", ""),
                rating=place.get("rating"),
                review_count=place.get("userRatingCount"),
                business_status=place.get("businessStatus", ""),
                source="google_places",
            )
        )
    return [r for r in results if r.name]


def _search_nominatim(query: str, limit: int) -> list[PlaceResult]:
    """
    OSM fallback. Far thinner data than Places -- typically no website and no
    review count -- but it needs no key and no billing account, which is the
    whole point of having it. Raises `ProviderError` for every failure, same
    contract as `_search_google`.
    """
    _nominatim_throttle()
    try:
        response = requests.get(
            NOMINATIM_SEARCH_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": min(limit, 50),
                "addressdetails": 1,
                "extratags": 1,
            },
            headers={"User-Agent": env("NOMINATIM_USER_AGENT", "leadgen-agent/0.1")},
            timeout=30,
        )
    except requests.exceptions.Timeout as exc:
        raise classify_exception(exc, provider="osm", operation=_NOMINATIM_OP) from exc
    except requests.exceptions.RequestException as exc:
        raise classify_exception(exc, provider="osm", operation=_NOMINATIM_OP) from exc

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise classify_http_status(
            429, response.text, provider="osm", operation=_NOMINATIM_OP,
            retry_after=float(retry_after) if retry_after else None,
        )
    if response.status_code != 200:
        raise classify_http_status(
            response.status_code, response.text, provider="osm", operation=_NOMINATIM_OP,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            ErrorCode.BAD_RESPONSE, provider="osm", operation=_NOMINATIM_OP,
            message=f"could not parse response as JSON: {exc}",
        ) from exc

    results: list[PlaceResult] = []
    for item in payload:
        extra = item.get("extratags") or {}
        name = (item.get("name") or "").strip()
        if not name:
            continue
        results.append(
            PlaceResult(
                name=name,
                address=item.get("display_name", ""),
                website=extra.get("website") or extra.get("contact:website") or "",
                phone=extra.get("phone") or extra.get("contact:phone") or "",
                category=item.get("type", ""),
                source="osm",
            )
        )
    return results


def search_osm_only(query: str, limit: int = 20) -> ProviderResult[list[PlaceResult]]:
    """
    OpenStreetMap only, regardless of whether a Google Places key is present.

    Used by the free `osm` discovery provider now that Google Maps is its own
    selectable, connectable provider (`google_places`) rather than a hidden
    preference this one used to apply on its own whenever a key happened to be
    in the environment. `search()` below still does that combined,
    Google-preferred-with-OSM-fallback behaviour -- it is what the
    `google_places` provider uses, so choosing Google Maps still degrades to
    OpenStreetMap on a Google-side failure instead of stopping outright.
    """
    try:
        results = call_with_retries(_search_nominatim, query, limit)
    except ProviderError as exc:
        log.error("OpenStreetMap discovery failed for %r: %s", query, exc)
        return ProviderResult.failure(exc, data=[])
    return ProviderResult.success("osm", _NOMINATIM_OP, results)


def search(query: str, limit: int = 20) -> ProviderResult[list[PlaceResult]]:
    """
    Search for local businesses, preferring Google Places and falling back to
    OSM whenever Places is unavailable for ANY reason -- no key, quota spent,
    or a failing request. Discovery degrading to thinner data beats discovery
    stopping, so a Google failure is not itself the result here -- it is
    recorded in `metadata` (a fallback must not hide the original failure)
    and OSM gets to answer. Only when BOTH have failed does this return
    `ERROR`; `[]` from either path alone means that provider searched and
    found nothing.
    """
    google_error: ProviderError | None = None

    if has_google_key():
        try:
            results = call_with_retries(_search_google, query, limit)
            return ProviderResult.success("google_places", _GOOGLE_OP, results)
        except QuotaExceeded as exc:
            log.warning("%s; falling back to OpenStreetMap", exc)
            google_error = ProviderError(
                ErrorCode.QUOTA_EXCEEDED, provider="google_places", operation=_GOOGLE_OP,
                message=str(exc),
            )
        except ProviderError as exc:
            log.warning("Google Places failed (%s); falling back to OpenStreetMap", exc)
            google_error = exc
    else:
        log.debug("no GOOGLE_PLACES_API_KEY set; using OpenStreetMap")

    metadata = {"google_places_error": google_error.code.value} if google_error else {}
    try:
        results = call_with_retries(_search_nominatim, query, limit)
    except ProviderError as osm_error:
        log.error("OpenStreetMap discovery also failed for %r: %s", query, osm_error)
        return ProviderResult.failure(osm_error, data=[], metadata=metadata)

    return ProviderResult.success("osm", _NOMINATIM_OP, results, metadata=metadata)


def build_query(search_term: str, location: str) -> str:
    """`dental clinic` + `Manchester` -> the text query both providers take."""
    return f"{search_term} in {location}".strip()
