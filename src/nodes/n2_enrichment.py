"""
N2 -- Enrichment.

In:  LeadState with website and/or linkedin_url
Out: contact_email, contact_name, signals[], unreachable

Scrapes the lead's site (BeautifulSoup; Playwright when the static fetch comes
back empty) honouring robots.txt, and derives niche-appropriate signals:

  local_business -> booking friction, no after-hours capture, no chat, stale
                    or non-responsive site, unmanaged reviews
  b2b            -> hiring activity, tech stack, decision-maker name and role

Hunter.io is called ONLY for the top-N highest-priority leads in a batch (N
from tenant config, conservative by default) and only when the scrape found no
address. The 25/month free-plan cap is enforced by a durable counter in
src/counters.py, not by hoping N stays small.

A lead with no email AND no LinkedIn URL is marked `unreachable`, which every
downstream conditional edge routes straight to archive.
"""

from __future__ import annotations

import re
from typing import Any

from src.integrations import scraping
from src.providers import enrichment_for
from src.providers.enrichment_adapters import HunterEnrichment
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node, retry_once
from src.state import LeadState, normalise_domain

# --------------------------------------------------------------------------- #
# Signal detection
# --------------------------------------------------------------------------- #

#: Marker -> the signal text emitted when the marker is ABSENT from the site.
#: Framed as gaps because a gap is what the outreach actually references.
_LOCAL_ABSENCE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "no online booking system on the site - enquiries depend on the phone",
        ("book online", "book now", "booking", "schedule online", "make an appointment",
         "request appointment", "reserve", "calendly", "acuity", "simplybook",
         "dentally", "cliniko", "fresha", "treatwell", "mindbody", "setmore",
         "squarespace-scheduling", "book an appointment"),
    ),
    (
        "no live chat or after-hours enquiry capture",
        ("livechat", "live chat", "tawk.to", "intercom", "drift", "crisp.chat",
         "hubspot-messages", "zendesk", "chatbot", "whatsapp"),
    ),
)

#: Marker -> the signal text emitted when the marker IS present.
_PRESENCE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("actively hiring - careers or vacancies page on the site",
     ("we are hiring", "we're hiring", "join our team", "careers", "vacancies",
      "current openings", "job opening", "now hiring")),
    ("runs a helpdesk or CRM with no AI deflection layer",
     ("zendesk", "intercom", "freshdesk", "hubspot", "front", "helpscout",
      "salesforce", "servicenow")),
    ("uses a page builder rather than a bespoke site",
     ("wix.com", "squarespace", "godaddy website builder", "weebly")),
)

_WORDPRESS_MARKERS = ("wp-content", "wp-includes", "/wp-json")
_VIEWPORT_RE = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.IGNORECASE)
_TEL_RE = re.compile(r'href=["\']tel:', re.IGNORECASE)


def detect_local_signals(html: str, text: str) -> list[str]:
    """Service-gap signals for a local business."""
    haystack = (html + " " + text).lower()
    signals: list[str] = []

    for signal, markers in _LOCAL_ABSENCE_SIGNALS:
        if not any(marker in haystack for marker in markers):
            signals.append(signal)

    if not _VIEWPORT_RE.search(html):
        signals.append("site has no mobile viewport tag - likely not mobile responsive")
    if not _TEL_RE.search(html) and "tel:" not in haystack:
        signals.append("phone number is not click-to-call on mobile")
    if any(marker in haystack for marker in _WORDPRESS_MARKERS):
        signals.append("WordPress site - straightforward to add booking and capture")
    if "@" not in haystack and "contact" not in haystack:
        signals.append("no contact route visible on the page at all")

    return signals


def detect_b2b_signals(html: str, text: str) -> list[str]:
    """Buying-intent signals for a B2B or SaaS company."""
    haystack = (html + " " + text).lower()
    signals: list[str] = []
    for signal, markers in _PRESENCE_SIGNALS:
        if any(marker in haystack for marker in markers):
            signals.append(signal)
    for marker, label in (
        ("series a", "recently raised a Series A"),
        ("series b", "recently raised a Series B"),
        ("seed round", "recently raised a seed round"),
        ("we raised", "announced a funding round"),
    ):
        if marker in haystack:
            signals.append(label)
            break
    for marker in ("customer success", "support team", "onboarding"):
        if marker in haystack:
            signals.append(f"site references {marker} as an operational function")
            break
    return signals


def match_icp_signals(text: str, icp: dict[str, Any]) -> list[str]:
    """
    Surface any of the niche's own `good_signals` that literally appear on the
    site. These outrank the generic detectors because the operator wrote them.
    """
    haystack = text.lower()
    hits: list[str] = []
    for signal in icp.get("good_signals", []) or []:
        phrase = str(signal).lower()
        keywords = [w for w in re.split(r"\W+", phrase) if len(w) > 4]
        if keywords and sum(word in haystack for word in keywords) >= max(
            1, len(keywords) // 2
        ):
            hits.append(str(signal))
    return hits


def detect_disqualifiers(text: str, icp: dict[str, Any]) -> list[str]:
    """Disqualifier phrases found verbatim on the site (fed to N3, not fatal)."""
    haystack = text.lower()
    return [
        str(d) for d in (icp.get("disqualifiers") or [])
        if str(d).lower() in haystack
    ]


# --------------------------------------------------------------------------- #
# Scraping one lead
# --------------------------------------------------------------------------- #

def scrape_lead(lead: LeadState, niche_type: str, icp: dict[str, Any]) -> dict[str, Any]:
    """
    Fetch a lead's site and extract contact details plus signals.

    Walks a small set of contact-ish paths and stops as soon as it has an
    address, so a five-page crawl only happens for the sites that actually
    need it.
    """
    website = lead.get("website") or ""
    if not website:
        return {"signals": [], "emails": [], "contact_name": "", "pages_fetched": 0}

    domain = normalise_domain(website)
    emails: list[str] = []
    contact_name = ""
    combined_html: list[str] = []
    combined_text: list[str] = []
    pages_fetched = 0

    for url in scraping.candidate_pages(website):
        page = scraping.fetch(url)
        if page.error == "robots_disallowed":
            log.info("robots.txt disallows %s; enrichment stops here", url)
            break
        if not page.ok:
            continue

        pages_fetched += 1
        html = page.html
        text = scraping.visible_text(html)

        # A page that renders its content in JS gives us almost no text; retry
        # that ONE page with a real browser rather than the whole site.
        if len(text) < 400 and pages_fetched == 1:
            rendered = scraping.fetch_rendered(url)
            if rendered.ok and len(scraping.visible_text(rendered.html)) > len(text):
                html = rendered.html
                text = scraping.visible_text(html)

        combined_html.append(html)
        combined_text.append(text)

        for address in scraping.extract_emails(html, prefer_domain=domain):
            if address not in emails:
                emails.append(address)
        if not contact_name:
            contact_name = scraping.extract_contact_name(html)
        if emails and contact_name:
            break

    html_blob = "\n".join(combined_html)
    text_blob = "\n".join(combined_text)

    if niche_type == "local_business":
        signals = detect_local_signals(html_blob, text_blob)
    else:
        signals = detect_b2b_signals(html_blob, text_blob)

    if not pages_fetched and website:
        signals.append("website did not respond - possible broken or parked domain")

    signals = match_icp_signals(text_blob, icp) + signals

    return {
        "signals": signals,
        "emails": emails,
        "contact_name": contact_name,
        "pages_fetched": pages_fetched,
        "disqualifiers": detect_disqualifiers(text_blob, icp),
    }


# --------------------------------------------------------------------------- #
# Rationing the metered lookups
# --------------------------------------------------------------------------- #

def select_lookup_candidates(
    leads: list[LeadState], top_n: int, provider: Any | None = None
) -> list[str]:
    """
    Choose which leads may spend one of the month's paid contact lookups.

    Priority: has a website (needed for a domain lookup), has no email yet,
    has no LinkedIn URL either (so a lookup is the ONLY way to reach them),
    then richer signals first. Capped by both the tenant's top_n and whatever
    the provider says is actually left -- asking for 5 when 2 remain must not
    silently burn the 2 on the wrong leads.

    This decision is comparative, so it happens once per batch rather than per
    lead: a single lead cannot know whether it is in the top 5 of a batch it
    cannot see.

    `provider` defaults to the metered lookup service rather than to the
    resolver because this function has no tenant config to resolve from. The
    batch runner, which does, passes the workspace's own choice -- so a
    workspace set to read websites only allocates nothing here instead of
    allocating five lookups against a service it has not connected.
    """
    provider = provider or HunterEnrichment()
    remaining = provider.budget_remaining()
    budget = max(0, min(top_n, remaining))
    if budget == 0:
        if top_n > 0:
            log.warning(
                "no contact lookups available (%s); none allocated this batch",
                provider.budget_status(),
            )
        return []

    def priority(lead: LeadState) -> tuple[int, int, int]:
        no_email = 0 if not lead.get("contact_email") else 1
        no_linkedin = 0 if not lead.get("linkedin_url") else 1
        return (no_email, no_linkedin, -len(lead.get("signals") or []))

    eligible = [
        lead for lead in leads
        if lead.get("website") and not lead.get("contact_email")
    ]
    eligible.sort(key=priority)
    chosen = [lead["lead_id"] for lead in eligible[:budget]]
    if chosen:
        log.info(
            "%d contact lookup(s) allocated this batch via %s (%s)",
            len(chosen), provider.id, provider.budget_status(),
        )
    return chosen


#: The name this had before the capability layer. Kept because the batch
#: runner and the tests import it, and because renaming a working entry point
#: buys nothing.
select_hunter_candidates = select_lookup_candidates


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n2_enrichment")
def n2_enrichment(
    state: LeadState,
    *,
    tenant_config: TenantConfig | None = None,
    hunter_allowed: bool | None = None,
) -> dict:
    """
    Enrich one lead.

    `hunter_allowed` is decided at BATCH level by `select_hunter_candidates`,
    because the top-N decision is inherently comparative -- a single lead
    cannot know whether it is in the top 5 of a batch it cannot see. The batch
    runner stamps the answer onto the lead as `_hunter_allowed` before the
    graph runs, since a graph node receives only state.

    In dry-run the fixture already supplies signals and contacts, so no network
    call is made at all; enrichment only fills the gaps.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])
    niche = config.niche(state["niche_id"])
    niche_type = niche.get("type", "local_business")
    icp = niche.get("icp") or {}
    if hunter_allowed is None:
        hunter_allowed = bool(state.get("_hunter_allowed", False))

    updates: dict[str, Any] = {}
    signals = list(state.get("signals") or [])
    contact_email = state.get("contact_email") or ""
    contact_name = state.get("contact_name") or ""

    if state.get("dry_run"):
        # Fixture data stands in for the scrape. Still exercise the signal
        # detectors against the ICP so dry runs test real logic, not a stub.
        if not signals:
            signals = ["no signals in fixture - N4 self-check should reject this lead"]
    elif state.get("website"):
        scraped = retry_once(
            scrape_lead, state, niche_type, icp, _label="scrape", _backoff_seconds=2.0
        )
        for signal in scraped["signals"]:
            if signal not in signals:
                signals.append(signal)
        if not contact_email and scraped["emails"]:
            contact_email = scraped["emails"][0]
            updates["source"] = state.get("source", "") or "scrape"
        if not contact_name and scraped["contact_name"]:
            contact_name = scraped["contact_name"]
        if scraped.get("disqualifiers"):
            signals.append(
                "DISQUALIFIER found on site: " + "; ".join(scraped["disqualifiers"])
            )

    # A paid lookup, only for the leads the batch selected and only if the
    # scrape did not already find an address.
    if hunter_allowed and not contact_email and state.get("website") and not state.get("dry_run"):
        lookup = enrichment_for(config, tenant_id=state["tenant_id"])
        first, _, last = (contact_name or "").partition(" ")
        result = lookup.find_email(
            normalise_domain(state["website"]),
            first_name=first if last else "",
            last_name=last,
        )
        if result:
            contact_email = result.email
            if not contact_name and result.full_name:
                contact_name = result.full_name
            if result.position:
                signals.append(f"decision-maker identified: {result.full_name} ({result.position})")
            # The provider's own name, not the adapter's: the CRM row records
            # where an address came from, and "hunter" and "custom" are
            # different provenance.
            updates["source"] = result.source or lookup.id

    if contact_email:
        updates["contact_email"] = contact_email.strip().lower()
    if contact_name:
        updates["contact_name"] = contact_name.strip()
    updates["signals"] = signals

    reachable = bool(contact_email or state.get("linkedin_url"))
    if not reachable:
        log.info(
            "unreachable tenant=%s lead=%s company=%r (no email, no LinkedIn URL)",
            state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        )
        updates["unreachable"] = True
        updates["archived"] = True
        updates["archive_reason"] = "unreachable"
        updates["next_action"] = "no contact route found - excluded from outreach"
    else:
        updates["unreachable"] = False

    return updates
