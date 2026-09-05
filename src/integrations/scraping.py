"""
scraping.py -- website fetch and parse helpers.

requests + BeautifulSoup for static pages, Playwright for JS-rendered ones.
robots.txt is fetched, cached and honoured before any request. Timeouts, polite
delays and a real User-Agent are enforced here rather than at each call site,
so no caller can accidentally hammer a small business's shared host.

Playwright is imported lazily: the package is a dependency but the browser
binaries are a separate ~150MB download (`playwright install chromium`). A
missing browser degrades to the static fetch with a warning instead of
crashing a batch.

Env: SCRAPER_USER_AGENT, SCRAPER_TIMEOUT_SECONDS, SCRAPER_MAX_PAGES_PER_SITE,
     SCRAPER_RESPECT_ROBOTS
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

from src.reliability import log
from src.settings import env, env_bool, env_int

#: Pages worth checking on a small business site, in priority order.
CONTACT_PATHS: tuple[str, ...] = (
    "", "/contact", "/contact-us", "/about", "/about-us", "/team",
    "/book", "/booking", "/appointments", "/kontakt", "/nous-contacter",
)

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.IGNORECASE
)
#: Addresses that are never a real human contact.
_EMAIL_JUNK = re.compile(
    r"(\.png|\.jpg|\.jpeg|\.gif|\.svg|\.webp|@sentry|@example\.|@sentry\.io"
    r"|wixpress|@2x|@3x|godaddy|@domain\.|your@|email@example)",
    re.IGNORECASE,
)
#: Local-parts that indicate a role-based address (preferred or required by
#: several compliance profiles -- see N5.5).
ROLE_LOCAL_PARTS: frozenset[str] = frozenset({
    "info", "hello", "contact", "enquiries", "enquiry", "inquiries", "admin",
    "office", "reception", "frontdesk", "front-desk", "bookings", "booking",
    "team", "mail", "hi", "support", "help", "sales", "kontakt", "praxis",
})

_PHONE_RE = re.compile(r"(\+?\d[\d\s().\-]{7,}\d)")


class RobotsDisallowed(RuntimeError):
    """The site's robots.txt forbids fetching this URL. Not an error to retry."""


@dataclass
class PageFetch:
    """One fetched page."""

    url: str
    status: int
    html: str = ""
    rendered: bool = False        # True if Playwright rendered it
    error: str = ""
    text: str = field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def user_agent() -> str:
    return env("SCRAPER_USER_AGENT", "leadgen-agent/0.1 (+https://example.com/bot)")


def timeout_seconds() -> int:
    return env_int("SCRAPER_TIMEOUT_SECONDS", 20)


def max_pages_per_site() -> int:
    return env_int("SCRAPER_MAX_PAGES_PER_SITE", 5)


def respect_robots() -> bool:
    return env_bool("SCRAPER_RESPECT_ROBOTS", True)


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=256)
def _robots_for(origin: str) -> urllib.robotparser.RobotFileParser | None:
    """
    Fetch and cache one origin's robots.txt.

    A robots.txt that cannot be fetched returns None, which we treat as
    "allowed" -- the same interpretation every mainstream crawler uses. A
    robots.txt that IS served and disallows us is honoured absolutely.
    """
    parser = urllib.robotparser.RobotFileParser()
    robots_url = urllib.parse.urljoin(origin, "/robots.txt")
    try:
        response = requests.get(
            robots_url, timeout=timeout_seconds(), headers={"User-Agent": user_agent()}
        )
        if response.status_code >= 400:
            return None
        parser.parse(response.text.splitlines())
        return parser
    except requests.RequestException as exc:
        log.debug("robots.txt unavailable for %s (%s); treating as allowed", origin, exc)
        return None


def is_allowed(url: str) -> bool:
    """True if robots.txt permits our User-Agent to fetch `url`."""
    if not respect_robots():
        return True
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    parser = _robots_for(origin)
    if parser is None:
        return True
    return parser.can_fetch(user_agent(), url)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def normalise_url(url: str) -> str:
    """Add a scheme if the config or a directory listing omitted one."""
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def fetch(url: str, *, delay: float = 0.5) -> PageFetch:
    """
    Fetch one page with requests. Raises nothing: transport failures come back
    as a PageFetch with `error` set, because a dead website is ordinary data
    about a lead, not an exception.
    """
    url = normalise_url(url)
    if not url:
        return PageFetch(url="", status=0, error="empty url")
    if not is_allowed(url):
        log.info("robots.txt disallows %s; skipping", url)
        return PageFetch(url=url, status=0, error="robots_disallowed")
    try:
        if delay:
            time.sleep(delay)          # politeness, not rate limiting
        response = requests.get(
            url,
            timeout=timeout_seconds(),
            headers={
                "User-Agent": user_agent(),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en;q=0.9,*;q=0.5",
            },
            allow_redirects=True,
        )
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type and response.status_code == 200:
            return PageFetch(url=url, status=response.status_code,
                             error=f"non-html content-type: {content_type}")
        return PageFetch(url=response.url, status=response.status_code, html=response.text)
    except requests.RequestException as exc:
        return PageFetch(url=url, status=0, error=f"{type(exc).__name__}: {exc}")


def fetch_rendered(url: str, *, wait_ms: int = 2500) -> PageFetch:
    """
    Fetch with Playwright, for sites that render their contact details in JS.

    Degrades to `fetch()` when the browser binaries are not installed -- the
    Python package alone is not enough, and a missing browser must not take
    down a batch.
    """
    url = normalise_url(url)
    if not is_allowed(url):
        return PageFetch(url=url, status=0, error="robots_disallowed")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed; falling back to static fetch for %s", url)
        return fetch(url)

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=user_agent())
                page.set_default_timeout(timeout_seconds() * 1000)
                response = page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(wait_ms)
                html = page.content()
                status = response.status if response else 200
                return PageFetch(url=url, status=status, html=html, rendered=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- includes missing-browser errors
        log.warning("playwright render failed for %s (%s); using static fetch", url, exc)
        result = fetch(url)
        result.error = result.error or f"playwright_failed: {exc}"
        return result


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def to_soup(html: str) -> BeautifulSoup:
    """Parse with lxml, falling back to the stdlib parser if lxml is absent."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:  # noqa: BLE001
        return BeautifulSoup(html, "html.parser")


def visible_text(html: str) -> str:
    soup = to_soup(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def extract_emails(html: str, *, prefer_domain: str = "") -> list[str]:
    """
    Pull candidate addresses out of a page, best first.

    Ordering matters: N2 takes the first one, and a role-based address on the
    company's own domain is both the most likely to be monitored and the one
    several compliance profiles require.
    """
    soup = to_soup(html)
    found: list[str] = []

    for anchor in soup.select('a[href^="mailto:"]'):
        href = anchor.get("href", "")
        address = href[len("mailto:"):].split("?")[0].strip()
        if address:
            found.append(address)

    found.extend(_EMAIL_RE.findall(html))

    seen: set[str] = set()
    cleaned: list[str] = []
    for address in found:
        address = address.strip().strip(".,;:").lower()
        if address in seen or _EMAIL_JUNK.search(address):
            continue
        if len(address) > 254 or address.count("@") != 1:
            continue
        seen.add(address)
        cleaned.append(address)

    domain = (prefer_domain or "").lower().removeprefix("www.")

    def rank(address: str) -> tuple[int, int, int]:
        local, _, host = address.partition("@")
        host = host.removeprefix("www.")
        on_domain = 0 if (domain and (host == domain or host.endswith("." + domain))) else 1
        role = 0 if local in ROLE_LOCAL_PARTS else 1
        return (on_domain, role, len(address))

    return sorted(cleaned, key=rank)


def extract_phones(text: str, limit: int = 3) -> list[str]:
    out: list[str] = []
    for match in _PHONE_RE.findall(text):
        candidate = " ".join(match.split())
        digits = sum(c.isdigit() for c in candidate)
        if 8 <= digits <= 15 and candidate not in out:
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def extract_contact_name(html: str) -> str:
    """
    Best-effort owner/practitioner name from a small business site. Deliberately
    conservative: a wrong name in a cold email is worse than no name at all, so
    anything ambiguous returns "" and N4 falls back to a nameless opening.
    """
    soup = to_soup(html)
    patterns = [
        re.compile(r"\b(?:Dr\.?|Doctor)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})"),
        re.compile(
            r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\s*[,\-–]\s*"
            r"(?:Owner|Founder|Principal|Practice Manager|Director|Head of)"
        ),
    ]
    text = " ".join(soup.get_text(" ", strip=True).split())[:6000]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return ""


def internal_links(html: str, base_url: str, limit: int = 12) -> list[str]:
    """Same-origin links, for the shallow contact-page crawl in N2."""
    soup = to_soup(html)
    base = urllib.parse.urlsplit(base_url)
    origin = f"{base.scheme}://{base.netloc}"
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base_url, anchor["href"]).split("#")[0]
        if not href.startswith(origin) or href in out or href == base_url:
            continue
        out.append(href)
        if len(out) >= limit:
            break
    return out


def is_role_based(email: str) -> bool:
    """True for info@ / hello@ / contact@ style addresses (see N5.5)."""
    if not email or "@" not in email:
        return False
    local = email.split("@", 1)[0].strip().lower()
    return local in ROLE_LOCAL_PARTS


def candidate_pages(website: str) -> list[str]:
    """The contact-ish URLs worth trying on a site, capped by config."""
    base = normalise_url(website)
    if not base:
        return []
    base = base.rstrip("/")
    urls = [base + path for path in CONTACT_PATHS]
    seen: set[str] = set()
    unique = [u for u in urls if not (u in seen or seen.add(u))]
    return unique[: max_pages_per_site()]
