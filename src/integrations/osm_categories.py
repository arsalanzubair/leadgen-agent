"""
osm_categories.py -- normalized business categories -> OpenStreetMap tags.

Nominatim is a geocoder with free-text search bolted on, not a business-
category search engine: asking it for "dental clinic in Berlin" works by
accident, when enough dentists happen to have put that phrase in their OSM
`name` tag, and fails silently the rest of the time. OpenStreetMap's own
answer to "find me every dentist in this area" is a structured query against
the tags surveyors actually attach to a place -- `amenity=dentist`,
`shop=hairdresser` -- run through the Overpass API, not Nominatim.

This table is what makes that possible: a category a niche's `search_terms`
might name, mapped to the tag(s) that mean it on OSM. `places.py` looks a term
up here first and only falls back to Nominatim's free-text search when the
term maps to nothing -- a category this table does not know about is still
searchable, just with thinner data, not silently unsearchable.

Extending this is the whole maintenance story: add a line. No code elsewhere
needs to change, because `tags_for_category` is the only way anything reads
this table.
"""

from __future__ import annotations

#: category phrase -> one or more (OSM key, OSM value) tags that identify it.
#: More than one pair means "any of these" -- a single business type that OSM
#: surveys under more than one tag (a beauty salon might be tagged
#: `shop=beauty` or `shop=hairdresser` depending on who mapped it).
CATEGORY_TAGS: dict[str, tuple[tuple[str, str], ...]] = {
    "dental clinic": (("amenity", "dentist"),),
    "dentist": (("amenity", "dentist"),),
    "orthodontist": (("amenity", "dentist"),),
    "restaurant": (("amenity", "restaurant"),),
    "cafe": (("amenity", "cafe"),),
    "coffee shop": (("amenity", "cafe"),),
    "bar": (("amenity", "bar"),),
    "pub": (("amenity", "pub"),),
    "bakery": (("shop", "bakery"),),
    "gym": (("leisure", "fitness_centre"),),
    "fitness studio": (("leisure", "fitness_centre"),),
    "personal training studio": (("leisure", "fitness_centre"),),
    "yoga studio": (("leisure", "fitness_centre"), ("sport", "yoga")),
    "hair salon": (("shop", "hairdresser"),),
    "hairdresser": (("shop", "hairdresser"),),
    "barber shop": (("shop", "hairdresser"), ("shop", "barber")),
    "beauty salon": (("shop", "beauty"), ("shop", "hairdresser")),
    "nail salon": (("shop", "beauty"),),
    "spa": (("leisure", "spa"), ("shop", "beauty")),
    "veterinary clinic": (("amenity", "veterinary"),),
    "vet": (("amenity", "veterinary"),),
    "pharmacy": (("amenity", "pharmacy"),),
    "chemist": (("amenity", "pharmacy"),),
    "law firm": (("office", "lawyer"),),
    "lawyer": (("office", "lawyer"),),
    "accounting firm": (("office", "accountant"),),
    "accountant": (("office", "accountant"),),
    "real estate agency": (("office", "estate_agent"),),
    "estate agent": (("office", "estate_agent"),),
    "insurance agency": (("office", "insurance"),),
    "auto repair shop": (("shop", "car_repair"),),
    "mechanic": (("shop", "car_repair"),),
    "car dealership": (("shop", "car"),),
    "hotel": (("tourism", "hotel"),),
    "guesthouse": (("tourism", "guest_house"),),
    "florist": (("shop", "florist"),),
    "bookshop": (("shop", "books"),),
    "bookstore": (("shop", "books"),),
    "hardware store": (("shop", "hardware"),),
    "furniture store": (("shop", "furniture"),),
    "clothing store": (("shop", "clothes"),),
    "shoe shop": (("shop", "shoes"),),
    "supermarket": (("shop", "supermarket"),),
    "grocery store": (("shop", "supermarket"), ("shop", "grocery")),
    "childcare centre": (("amenity", "childcare"),),
    "daycare": (("amenity", "childcare"),),
    "physiotherapy clinic": (("healthcare", "physiotherapist"), ("amenity", "clinic")),
    "chiropractor": (("healthcare", "chiropractor"),),
    "optician": (("shop", "optician"),),
    "dry cleaner": (("shop", "dry_cleaning"),),
    "laundromat": (("shop", "laundry"),),
    "locksmith": (("craft", "locksmith"), ("shop", "locksmith")),
    "electrician": (("craft", "electrician"),),
    "plumber": (("craft", "plumber"),),
    "machine shop": (("craft", "metal_construction"),),
    "print shop": (("shop", "copyshop"), ("craft", "photographer")),
    "photography studio": (("craft", "photographer"), ("shop", "photo")),
}


def normalise(term: str) -> str:
    return " ".join((term or "").strip().lower().split())


def tags_for_category(term: str) -> tuple[tuple[str, str], ...] | None:
    """
    The OSM tags for one category phrase, or `None` when this table has no
    structured mapping for it -- the caller's signal to fall back to a
    free-text search rather than fail outright.

    Tries an exact match, then the singular form (a niche's `search_terms`
    are typically plural -- "dental clinics", "hair salons"), then a loose
    containment match so "dental clinic near me" or "local dentist" still
    resolves without every possible phrasing needing its own entry.
    """
    normalised = normalise(term)
    if not normalised:
        return None
    if normalised in CATEGORY_TAGS:
        return CATEGORY_TAGS[normalised]
    if normalised.endswith("s") and normalised[:-1] in CATEGORY_TAGS:
        return CATEGORY_TAGS[normalised[:-1]]
    for key, tags in CATEGORY_TAGS.items():
        if key in normalised or normalised in key:
            return tags
    return None
