"""
anymail_finder.py -- email finding through Anymail Finder.

The same job as `hunter.py` and, deliberately, the same shape: one lookup per
call, counted against a durable monthly counter before the call is made, and a
result discarded rather than returned when the service is not confident.

Two differences from Hunter worth knowing:

  * Anymail Finder charges for VERIFIED results only. A 404 ("not found") does
    not cost a credit. The counter here still counts every attempt, because a
    counter that only counts the successful ones cannot stop a runaway loop --
    it errs towards the user's wallet, and the number it reports is attempts,
    which is what a person watching a budget wants to know.
  * It answers with a validation verdict rather than a 0-100 score. "valid"
    maps to full confidence; "risky" is treated as below the floor and dropped,
    because a risky address is a bounce, and a bounce costs sender reputation.

Env: ANYMAIL_FINDER_API_KEY, ANYMAIL_FINDER_MONTHLY_LOOKUP_CAP
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from src.counters import Counter, QuotaExceeded
from src.reliability import log, retry_once
from src.settings import env, env_int

SEARCH_URL = "https://api.anymailfinder.com/v5.0/search/person.json"
COMPANY_URL = "https://api.anymailfinder.com/v5.0/search/company.json"

#: Their verdicts, as confidence. "risky" is deliberately below the floor used
#: everywhere else in this system (70), so it is dropped rather than sent to.
_CONFIDENCE = {"valid": 95, "risky": 40, "unknown": 0}

MIN_CONFIDENCE = 70


@dataclass
class AnymailResult:
    email: str
    confidence: int = 0
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    source: str = "anymail_finder"


def has_api_key() -> bool:
    return bool(env("ANYMAIL_FINDER_API_KEY"))


def monthly_cap() -> int:
    """
    Anymail Finder has no free tier, so this cap is not a plan limit -- it is
    the user's own ceiling on what a runaway batch can spend. It defaults low
    on purpose; raising it is a deliberate act.
    """
    return env_int("ANYMAIL_FINDER_MONTHLY_LOOKUP_CAP", 100)


def counter() -> Counter:
    return Counter("anymail_finder_lookups", cap=monthly_cap(), period="month")


def remaining_lookups() -> int:
    return counter().remaining


def quota_status() -> str:
    return counter().status()


def find_email(
    domain: str, *, first_name: str = "", last_name: str = ""
) -> AnymailResult | None:
    """
    One lookup. Returns None for anything not worth sending to.

    The counter is spent BEFORE the request, and is not refunded on failure --
    the same rule Hunter follows, for the same reason: a call that errored may
    still have been billed, and a counter that under-counts is not a cap.
    """
    api_key = env("ANYMAIL_FINDER_API_KEY")
    if not api_key or not domain:
        return None

    budget = counter()
    try:
        budget.check(1)
    except QuotaExceeded as exc:
        log.warning("Anymail Finder lookup skipped: %s", exc)
        return None
    budget.consume(1)

    full_name = " ".join(part for part in (first_name, last_name) if part)
    url = SEARCH_URL if full_name else COMPANY_URL
    payload = {"domain": domain}
    if full_name:
        payload["full_name"] = full_name

    def call() -> dict:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        # Their "we looked and found nobody" answer. Not an error, and not
        # worth a retry.
        if response.status_code in (404, 451):
            return {}
        if response.status_code == 429:
            raise RuntimeError("Anymail Finder rate limit hit (429)")
        if response.status_code >= 400:
            raise RuntimeError(
                f"Anymail Finder returned {response.status_code}: {response.text[:200]}"
            )
        return response.json() or {}

    try:
        data = retry_once(call, _label="anymail_finder")
    except Exception as exc:  # noqa: BLE001
        log.warning("Anymail Finder lookup failed for %s: %s", domain, exc)
        return None

    results = data.get("results") or data
    email = str(results.get("email") or "").strip()
    if not email:
        return None

    verdict = str(results.get("validation") or results.get("email_class") or "").lower()
    confidence = _CONFIDENCE.get(verdict, 0)
    if confidence < MIN_CONFIDENCE:
        log.info(
            "Anymail Finder returned %s for %s but rated it %r; not using it",
            email, domain, verdict or "unrated",
        )
        return None

    return AnymailResult(
        email=email,
        confidence=confidence,
        first_name=str(results.get("person_first_name") or first_name or ""),
        last_name=str(results.get("person_last_name") or last_name or ""),
        position=str(results.get("person_job_title") or ""),
    )
