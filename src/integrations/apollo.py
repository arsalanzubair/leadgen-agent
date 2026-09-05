"""
apollo.py -- B2B and SaaS discovery.

Two paths, one output shape:

  * Apollo.io free plan `mixed_people/search`, when APOLLO_API_KEY is set.
  * Manual CSV import, for LinkedIn Sales Navigator exports done by hand. The
    free Apollo plan is thin, and Sales Navigator has no free API at all, so
    hand-exporting a list and dropping it in a folder is a first-class path
    here rather than an afterthought.

The CSV reader is deliberately forgiving about headers: exports from Apollo,
Sales Navigator and a hand-made spreadsheet all use different column names for
the same five things.

Env: APOLLO_API_KEY, APOLLO_CSV_IMPORT_DIR
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from src.reliability import log, retry_once
from src.settings import ROOT, env

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"


@dataclass
class ContactResult:
    """One discovered B2B contact, provider-agnostic."""

    company_name: str
    contact_name: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    website: str = ""
    location: str = ""
    industry: str = ""
    employee_count: int | None = None
    source: str = "apollo"


# --------------------------------------------------------------------------- #
# CSV import
# --------------------------------------------------------------------------- #

#: Header aliases seen across Apollo exports, Sales Navigator exports and
#: hand-built sheets. Lowercased and stripped of punctuation before matching.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company_name": (
        "company", "company name", "organization", "organization name",
        "account name", "employer",
    ),
    "contact_name": ("name", "full name", "contact", "contact name", "person"),
    "first_name": ("first name", "firstname", "given name"),
    "last_name": ("last name", "lastname", "surname", "family name"),
    "title": ("title", "job title", "position", "role", "headline"),
    "email": ("email", "email address", "work email", "primary email"),
    "linkedin_url": (
        "linkedin", "linkedin url", "person linkedin url", "linkedin profile",
        "profile url", "linkedin profile url",
    ),
    "website": ("website", "company website", "domain", "company domain", "url"),
    "location": ("location", "city", "country", "person location", "geography"),
    "industry": ("industry", "sector", "company industry"),
    "employee_count": (
        "employees", "employee count", "# employees", "company size", "headcount",
    ),
}


def _normalise_header(header: str) -> str:
    return " ".join(str(header or "").strip().lower().replace("_", " ").split())


def _build_header_map(fieldnames: Iterable[str]) -> dict[str, str]:
    """Map our canonical field names onto whatever this CSV happens to call them."""
    lookup = {_normalise_header(name): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in (canonical.replace("_", " "), *aliases):
            if alias in lookup:
                mapping[canonical] = lookup[alias]
                break
    return mapping


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", "").split("-")[0].strip())
    except (TypeError, ValueError):
        return None


def read_csv(path: Path) -> list[ContactResult]:
    """
    Read one hand-exported CSV into ContactResults.

    A row with neither an email nor a LinkedIn URL is dropped here rather than
    downstream: it would be marked unreachable by N2 anyway, and dropping it
    now keeps the low-yield signal honest.
    """
    if not path.exists():
        log.warning("CSV import path does not exist: %s", path)
        return []

    results: list[ContactResult] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            log.warning("CSV %s has no header row", path.name)
            return []
        mapping = _build_header_map(reader.fieldnames)
        if "company_name" not in mapping:
            log.warning(
                "CSV %s has no recognisable company column (headers: %s)",
                path.name, reader.fieldnames,
            )
            return []

        for row in reader:
            def get(field: str) -> str:
                column = mapping.get(field)
                return (row.get(column) or "").strip() if column else ""

            name = get("contact_name")
            if not name:
                name = " ".join(x for x in (get("first_name"), get("last_name")) if x)

            company = get("company_name")
            email = get("email").lower()
            linkedin = get("linkedin_url")
            if not company or (not email and not linkedin):
                continue

            results.append(
                ContactResult(
                    company_name=company,
                    contact_name=name,
                    title=get("title"),
                    email=email,
                    linkedin_url=linkedin,
                    website=get("website"),
                    location=get("location"),
                    industry=get("industry"),
                    employee_count=_as_int(get("employee_count")),
                    source="csv",
                )
            )
    log.info("imported %d contact(s) from %s", len(results), path.name)
    return results


def csv_import_dir() -> Path:
    raw = env("APOLLO_CSV_IMPORT_DIR", "./data/imports")
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def import_csvs(pattern: str = "*.csv") -> list[ContactResult]:
    """Read every CSV in the import directory matching `pattern`."""
    directory = csv_import_dir()
    if not directory.exists():
        log.debug("no CSV import directory at %s", directory)
        return []
    results: list[ContactResult] = []
    for path in sorted(directory.glob(pattern)):
        results.extend(read_csv(path))
    return results


# --------------------------------------------------------------------------- #
# Apollo API
# --------------------------------------------------------------------------- #

def has_api_key() -> bool:
    return bool(env("APOLLO_API_KEY"))


def _search_apollo(
    titles: list[str],
    locations: list[str],
    industries: list[str],
    employee_range: list[int] | None,
    limit: int,
) -> list[ContactResult]:
    payload: dict[str, Any] = {
        "page": 1,
        "per_page": min(limit, 25),
    }
    if titles:
        payload["person_titles"] = titles
    if locations:
        payload["person_locations"] = locations
    if industries:
        payload["q_organization_industry_tag_ids"] = industries
    if employee_range and len(employee_range) == 2:
        payload["organization_num_employees_ranges"] = [
            f"{employee_range[0]},{employee_range[1]}"
        ]

    response = requests.post(
        APOLLO_SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": env("APOLLO_API_KEY"),
        },
        json=payload,
        timeout=45,
    )
    if response.status_code == 403:
        raise RuntimeError(
            "Apollo returned 403 -- the free plan does not include API search "
            "on all accounts. Use the CSV import path instead."
        )
    if response.status_code != 200:
        raise RuntimeError(f"Apollo returned {response.status_code}: {response.text[:300]}")

    results: list[ContactResult] = []
    for person in response.json().get("people", []):
        org = person.get("organization") or {}
        # Apollo's free plan usually redacts the email as "email_not_unlocked@..."
        raw_email = (person.get("email") or "").strip().lower()
        email = "" if "not_unlocked" in raw_email else raw_email
        company = (org.get("name") or person.get("organization_name") or "").strip()
        if not company:
            continue
        results.append(
            ContactResult(
                company_name=company,
                contact_name=(person.get("name") or "").strip(),
                title=(person.get("title") or "").strip(),
                email=email,
                linkedin_url=(person.get("linkedin_url") or "").strip(),
                website=(org.get("website_url") or "").strip(),
                location=", ".join(
                    x for x in (person.get("city"), person.get("country")) if x
                ),
                industry=(org.get("industry") or "").strip(),
                employee_count=org.get("estimated_num_employees"),
                source="apollo",
            )
        )
    return results


def search(
    *,
    titles: list[str] | None = None,
    locations: list[str] | None = None,
    industries: list[str] | None = None,
    employee_range: list[int] | None = None,
    limit: int = 25,
    csv_glob: str = "",
) -> list[ContactResult]:
    """
    Discover B2B contacts, preferring the API and falling back to CSV import.

    Both paths are additive rather than exclusive when the API returns nothing:
    an empty API result with CSVs sitting in the import folder should still
    produce leads.
    """
    results: list[ContactResult] = []

    if has_api_key():
        try:
            results = retry_once(
                _search_apollo,
                titles or [], locations or [], industries or [],
                employee_range, limit, _label="apollo_search",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Apollo API search failed (%s); trying CSV import", exc)
    else:
        log.debug("no APOLLO_API_KEY set; using CSV import only")

    if len(results) < limit and csv_glob:
        results.extend(import_csvs(csv_glob))

    return results[:limit] if limit else results
