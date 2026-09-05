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
from src.reliability import log, retry_once
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


def _search_google(query: str, limit: int) -> list[PlaceResult]:
    counter = places_counter()
    counter.check(1)          # raises QuotaExceeded before spending the credit

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
    counter.consume(1)
    if response.status_code != 200:
        raise RuntimeError(
            f"Google Places returned {response.status_code}: {response.text[:300]}"
        )

    results: list[PlaceResult] = []
    for place in response.json().get("places", []):
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
    whole point of having it.
    """
    _nominatim_throttle()
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
    if response.status_code != 200:
        raise RuntimeError(
            f"Nominatim returned {response.status_code}: {response.text[:200]}"
        )

    results: list[PlaceResult] = []
    for item in response.json():
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


def search(query: str, limit: int = 20) -> list[PlaceResult]:
    """
    Search for local businesses, preferring Google Places and falling back to
    OSM whenever Places is unavailable for ANY reason -- no key, quota spent,
    or a failing request. Discovery degrading to thinner data beats discovery
    stopping.
    """
    if has_google_key():
        try:
            return retry_once(_search_google, query, limit, _label="google_places")
        except QuotaExceeded as exc:
            log.warning("%s; falling back to OpenStreetMap", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Google Places failed (%s); falling back to OpenStreetMap", exc)
    else:
        log.debug("no GOOGLE_PLACES_API_KEY set; using OpenStreetMap")

    try:
        return retry_once(_search_nominatim, query, limit, _label="nominatim")
    except Exception as exc:  # noqa: BLE001
        log.error("OpenStreetMap discovery also failed for %r: %s", query, exc)
        return []


def build_query(search_term: str, location: str) -> str:
    """`dental clinic` + `Manchester` -> the text query both providers take."""
    return f"{search_term} in {location}".strip()
