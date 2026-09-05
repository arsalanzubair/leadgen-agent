"""
hunter.py -- email finding, hard-capped at the free plan's 25 lookups/month.

Section 1 is explicit that a free tier with a hard numeric limit must be
enforced in code with a counter, not a comment. That enforcement lives in
`src.counters.Counter`, which persists the running count and the period key to
disk atomically, so the cap survives process restarts and cannot be reset by a
crash mid-write.

Two guards, deliberately:
  1. the durable monthly counter (25/month, free plan), and
  2. N2 only calls this for the top-N highest-priority leads in a batch.

A QuotaExceeded is a normal outcome here, not a failure: `find_email` returns
None and N2 continues with whatever the scrape found.

Env: HUNTER_API_KEY, HUNTER_MONTHLY_LOOKUP_CAP, HUNTER_QUOTA_FILE
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

from src.counters import QuotaExceeded, hunter_counter
from src.reliability import log, retry_once
from src.settings import env

DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
EMAIL_FINDER_URL = "https://api.hunter.io/v2/email-finder"

#: Hunter's own confidence score below which we would rather send nothing.
#: A bounced cold email costs sender reputation, which is far more expensive
#: than one skipped lead.
MIN_CONFIDENCE = 70


@dataclass
class HunterResult:
    email: str
    confidence: int = 0
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    source: str = "hunter"

    @property
    def full_name(self) -> str:
        return " ".join(x for x in (self.first_name, self.last_name) if x)


def has_api_key() -> bool:
    return bool(env("HUNTER_API_KEY"))


def remaining_lookups() -> int:
    """How many lookups are left this month. Read by N2 to size its top-N."""
    return hunter_counter().remaining


def quota_status() -> str:
    return hunter_counter().status()


def _domain_search(domain: str) -> HunterResult | None:
    response = requests.get(
        DOMAIN_SEARCH_URL,
        params={"domain": domain, "api_key": env("HUNTER_API_KEY"), "limit": 10},
        timeout=30,
    )
    if response.status_code == 429:
        raise RuntimeError("Hunter rate limit hit (429)")
    if response.status_code != 200:
        raise RuntimeError(f"Hunter returned {response.status_code}: {response.text[:200]}")

    payload = response.json().get("data") or {}
    emails = payload.get("emails") or []
    if not emails:
        return None

    def rank(entry: dict) -> tuple[int, int]:
        # Prefer a decision-maker over a generic address, then higher confidence.
        seniority = (entry.get("seniority") or "").lower()
        department = (entry.get("department") or "").lower()
        priority = 0 if seniority in ("executive", "senior") else 1
        if department in ("management", "executive"):
            priority = min(priority, 0)
        return (priority, -int(entry.get("confidence") or 0))

    best = sorted(emails, key=rank)[0]
    return HunterResult(
        email=(best.get("value") or "").lower(),
        confidence=int(best.get("confidence") or 0),
        first_name=best.get("first_name") or "",
        last_name=best.get("last_name") or "",
        position=best.get("position") or "",
    )


def _email_finder(domain: str, first_name: str, last_name: str) -> HunterResult | None:
    response = requests.get(
        EMAIL_FINDER_URL,
        params={
            "domain": domain,
            "first_name": first_name,
            "last_name": last_name,
            "api_key": env("HUNTER_API_KEY"),
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Hunter returned {response.status_code}: {response.text[:200]}")
    payload = response.json().get("data") or {}
    email = (payload.get("email") or "").lower()
    if not email:
        return None
    return HunterResult(
        email=email,
        confidence=int(payload.get("score") or 0),
        first_name=payload.get("first_name") or first_name,
        last_name=payload.get("last_name") or last_name,
        position=payload.get("position") or "",
    )


def find_email(
    domain: str,
    *,
    first_name: str = "",
    last_name: str = "",
    min_confidence: int = MIN_CONFIDENCE,
) -> HunterResult | None:
    """
    Find one address for `domain`, spending exactly one lookup from the monthly
    budget when a call is actually made.

    Returns None -- never raises -- for every "we could not do this" case: no
    key, quota spent, nothing found, or confidence below the floor. N2 treats
    all of them identically, which is the point.
    """
    domain = (domain or "").strip().lower().removeprefix("www.")
    if not domain:
        return None
    if not has_api_key():
        log.debug("no HUNTER_API_KEY set; skipping email lookup for %s", domain)
        return None

    counter = hunter_counter()
    try:
        counter.check(1)
    except QuotaExceeded as exc:
        log.warning("%s -- skipping Hunter lookup for %s", exc, domain)
        return None

    try:
        if first_name and last_name:
            result = retry_once(
                _email_finder, domain, first_name, last_name, _label="hunter_finder"
            )
        else:
            result = retry_once(_domain_search, domain, _label="hunter_domain_search")
    except Exception as exc:  # noqa: BLE001
        # A failed call may still have been billed; count it rather than risk
        # overrunning the free plan.
        counter.consume(1)
        log.warning("Hunter lookup failed for %s (%s); counted against quota", domain, exc)
        return None

    used = counter.consume(1)
    log.info("hunter lookup %d/%d used (domain=%s)", used, counter.cap, domain)

    if result is None:
        return None
    if result.confidence < min_confidence:
        log.info(
            "hunter found %s for %s but confidence %d < %d; discarding",
            result.email, domain, result.confidence, min_confidence,
        )
        return None
    return result
