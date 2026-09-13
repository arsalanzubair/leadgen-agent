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

from src.providers.results import (
    ErrorCode,
    ProviderError,
    ProviderResult,
    call_with_retries,
    classify_exception,
    classify_http_status,
)
from src.reliability import log
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


_OPERATION = "discover_contacts"


def _search_apollo(
    titles: list[str],
    locations: list[str],
    industries: list[str],
    employee_range: list[int] | None,
    limit: int,
) -> list[ContactResult]:
    """
    One call to `mixed_people/search`. Raises `ProviderError` for every
    failure -- never returns `[]` to mean anything but "Apollo answered and
    there was nobody matching this."
    """
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

    try:
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
    except requests.exceptions.Timeout as exc:
        raise classify_exception(exc, provider="apollo", operation=_OPERATION) from exc
    except requests.exceptions.RequestException as exc:
        raise classify_exception(exc, provider="apollo", operation=_OPERATION) from exc

    if response.status_code == 403:
        # Named and documented rather than left to the generic 403 mapping:
        # this is the single most common way this call fails, and "the free
        # plan does not include API search on every account" is a fact a user
        # can act on, where "permission denied" on its own is not.
        raise ProviderError(
            ErrorCode.PLAN_LIMITATION,
            provider="apollo",
            operation=_OPERATION,
            message=f"403: {response.text[:200]}",
            user_message="Apollo's free plan does not include API contact search on every account.",
            user_action="Use the CSV import folder instead, or upgrade the Apollo plan.",
            retryable=False,
            http_status=403,
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise classify_http_status(
            429, response.text, provider="apollo", operation=_OPERATION,
            retry_after=float(retry_after) if retry_after else None,
        )
    if response.status_code != 200:
        raise classify_http_status(
            response.status_code, response.text, provider="apollo", operation=_OPERATION,
        )

    try:
        payload_json = response.json()
    except ValueError as exc:
        raise ProviderError(
            ErrorCode.BAD_RESPONSE, provider="apollo", operation=_OPERATION,
            message=f"could not parse response as JSON: {exc}",
        ) from exc

    results: list[ContactResult] = []
    for person in payload_json.get("people", []):
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
) -> ProviderResult[list[ContactResult]]:
    """
    Discover B2B contacts, preferring the API and falling back to CSV import.

    Both paths are additive rather than exclusive when the API returns nothing:
    an empty API result with CSVs sitting in the import folder should still
    produce leads. A genuine API failure (Section 9's "plan limitation ->
    don't endlessly retry -> use a valid fallback if available") tries CSV
    import too, before giving up -- but if CSV has nothing either, the
    original failure is what comes back, not a bare `[]`. No API key at all
    is a configuration fact, not a failure: CSV-only has always been how a
    keyless install produces B2B leads.
    """
    if not has_api_key():
        log.debug("no APOLLO_API_KEY set; using CSV import only")
        rows = import_csvs(csv_glob) if csv_glob else []
        return ProviderResult.success(
            "apollo", _OPERATION, rows[:limit] if limit else rows,
            metadata={"source": "csv_only"},
        )

    try:
        results = call_with_retries(
            _search_apollo,
            titles or [], locations or [], industries or [],
            employee_range, limit,
        )
    except ProviderError as err:
        log.warning("Apollo API search failed (%s); trying CSV import", err)
        if csv_glob:
            fallback_rows = import_csvs(csv_glob)
            if fallback_rows:
                return ProviderResult.success(
                    "apollo", _OPERATION,
                    fallback_rows[:limit] if limit else fallback_rows,
                    metadata={"fallback": "csv_import", "original_error": err.code.value},
                )
        return ProviderResult.failure(err)

    if len(results) < limit and csv_glob:
        results = results + import_csvs(csv_glob)

    return ProviderResult.success("apollo", _OPERATION, results[:limit] if limit else results)
