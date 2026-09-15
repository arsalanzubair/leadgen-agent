"""
The category -> OSM tag table `places.py` uses for structured discovery.

A niche's `search_terms` are written by a person (or drafted by a model from
a person's plain English), so they are plural, loosely worded, and never
exactly the key an engineer would have chosen. This table has to tolerate
that, or "dental clinics" -- the plural form the fixture and the drafting
prompt both actually produce -- would never hit its own mapping.
"""

from __future__ import annotations

from src.integrations.osm_categories import tags_for_category


def test_an_exact_match_resolves():
    assert tags_for_category("dentist") == (("amenity", "dentist"),)


def test_the_plural_form_resolves_to_the_singular_entry():
    assert tags_for_category("dental clinics") == (("amenity", "dentist"),)
    assert tags_for_category("restaurants") == (("amenity", "restaurant"),)


def test_a_loose_phrase_containing_a_known_category_resolves():
    assert tags_for_category("local dentist near me") == (("amenity", "dentist"),)


def test_case_and_whitespace_do_not_matter():
    assert tags_for_category("  Hair   Salon  ") == tags_for_category("hair salon")


def test_an_unmapped_category_returns_none_rather_than_a_guess():
    """
    The whole point: a category this table does not recognise must say so
    plainly, so the caller can fall back to free text, rather than silently
    matching the wrong tag.
    """
    assert tags_for_category("underwater basket weaving supplier") is None


def test_an_empty_term_returns_none():
    assert tags_for_category("") is None
    assert tags_for_category("   ") is None


def test_every_entry_maps_to_at_least_one_real_tag_pair():
    from src.integrations.osm_categories import CATEGORY_TAGS

    for category, tags in CATEGORY_TAGS.items():
        assert tags, f"{category!r} maps to no tags at all"
        for key, value in tags:
            assert key and value, f"{category!r} has a blank tag: {(key, value)!r}"
