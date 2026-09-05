"""
N1 -- Discovery.

In:  BatchTarget(niche_id, region) + the tenant config
Out: seed LeadStates (company_name, website / linkedin_url, location, region,
     niche_id, source)

Which provider actually finds them is the workspace's choice, not this node's:
a niche's `type` selects the discovery capability (local business or business
contacts) and the tenant's `providers:` block selects who serves it. This node
builds a `DiscoveryRequest` from the niche and turns whatever comes back into
leads, so adding a fourth source never touches this file.

Deduplicates against everything this tenant has previously discovered, using
state.make_dedupe_key (domain match, else normalised name + location). The
ledger is per tenant (settings.seen_leads_path) -- tenant A rediscovering a
company tenant B already has is a NEW lead, because they are separate books of
business.

Yield below the configured threshold tags the batch `low_yield` in the logs and
in the batch result rather than failing: a thin region is information, not an
error, and failing the batch would also discard the regions that did work.

STRUCTURE NOTE -- why there are two entry points here
-----------------------------------------------------
`discover()` is batch-level: it takes a target and returns MANY leads. The
graph, per Section 5, is `StateGraph(LeadState)` -- one thread per lead -- so a
node inside it cannot fan one state out into many. `n1_discovery()` is
therefore the in-graph node: it registers the already-seeded lead in the dedupe
ledger and is a no-op on re-entry. cli/run_batch.py calls `discover()` first,
then opens one graph thread per resulting lead.
"""

from __future__ import annotations

import json
from typing import Any

from src.nodes.n0_config_load import TenantConfig
from src.providers import DiscoveredBusiness, DiscoveryRequest, discovery_for
from src.reliability import SkipLead, log, node
from src.settings import SAMPLE_LEADS_PATH, seen_leads_path
from src.state import (
    BatchTarget,
    LeadState,
    make_dedupe_key,
    make_lead_id,
    new_lead_state,
    normalise_domain,
)

# --------------------------------------------------------------------------- #
# Per-tenant dedupe ledger
# --------------------------------------------------------------------------- #

def load_seen(tenant_id: str) -> dict[str, Any]:
    """Every dedupe_key this tenant has already discovered, with first-seen dates."""
    path = seen_leads_path(tenant_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def remember_seen(tenant_id: str, entries: dict[str, Any]) -> None:
    if not entries:
        return
    path = seen_leads_path(tenant_id)
    current = load_seen(tenant_id)
    current.update(entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")


def forget_all(tenant_id: str) -> None:
    """Wipe a tenant's dedupe ledger. For tests and deliberate re-discovery."""
    seen_leads_path(tenant_id).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# DiscoveredBusiness -> LeadState
# --------------------------------------------------------------------------- #

def _discovery_signals(found: DiscoveredBusiness, kind: str) -> list[str]:
    """
    What the search itself already told us, before N2 reads their website.

    Keyed on the KIND of search rather than on which fields happen to be
    populated. "No website listed on their business profile" is a real buying
    signal for a dental practice found on a map and meaningless for a Head of
    Support found in a contact database -- the absence means "they have no web
    presence" in the first case and "this record does not carry one" in the
    second. Deriving these purely from which fields are non-empty would put
    the wrong one on the wrong lead.
    """
    signals: list[str] = []

    if kind == "local_business":
        if not found.website:
            signals.append("no website listed on their business profile")
        if found.review_count is not None and found.rating is not None:
            signals.append(
                f"{found.review_count} reviews averaging {found.rating} on their listing"
            )
        if found.phone:
            signals.append(f"public phone number {found.phone}")
        return signals

    if found.title:
        signals.append(
            f"decision-maker identified: {found.contact_name} ({found.title})"
        )
    if found.employee_count:
        signals.append(f"approximately {found.employee_count} employees")
    return signals


def _to_lead(
    found: DiscoveredBusiness,
    target: BatchTarget,
    tenant_id: str,
    dry_run: bool,
    kind: str,
) -> LeadState:
    """One discovered business as a seed lead."""
    lead = new_lead_state(
        tenant_id=tenant_id,
        niche_id=target["niche_id"],
        region=target["region"],
        language=target["language"],
        company_name=found.company_name,
        website=found.website,
        linkedin_url=found.linkedin_url,
        location=found.where,
        industry=found.industry or found.category,
        contact_email=found.contact_email,
        contact_name=found.contact_name,
        source=found.source,
        dry_run=dry_run,
    )
    lead["signals"] = _discovery_signals(found, kind)
    return lead


# --------------------------------------------------------------------------- #
# Dry-run source
# --------------------------------------------------------------------------- #

def load_fixture_leads(
    tenant_id: str, targets: list[BatchTarget] | None = None
) -> list[LeadState]:
    """
    Build seed leads from tests/fixtures/sample_leads.json instead of calling a
    live provider. This is what makes `--dry-run` work with zero API keys.

    The fixture's `_expect` annotations are dropped here; they document intent
    for a human reading the file, they are not part of the data contract.
    """
    payload = json.loads(SAMPLE_LEADS_PATH.read_text(encoding="utf-8"))
    wanted = {(t["niche_id"], t["region"]) for t in targets} if targets else None

    leads: list[LeadState] = []
    for entry in payload.get("leads", []):
        pair = (entry.get("niche_id"), entry.get("region"))
        if wanted is not None and pair not in wanted:
            continue
        lead = new_lead_state(
            tenant_id=tenant_id,
            niche_id=entry["niche_id"],
            region=entry["region"],
            language=entry.get("language", "en"),
            company_name=entry.get("company_name", ""),
            website=entry.get("website", ""),
            linkedin_url=entry.get("linkedin_url", ""),
            location=entry.get("location", ""),
            industry=entry.get("industry", ""),
            contact_email=entry.get("contact_email", ""),
            contact_name=entry.get("contact_name", ""),
            source="fixture",
            dry_run=True,
        )
        lead["signals"] = list(entry.get("signals") or [])
        leads.append(lead)
    return leads


# --------------------------------------------------------------------------- #
# Batch-level discovery
# --------------------------------------------------------------------------- #

def discover_for_target(
    config: TenantConfig, target: BatchTarget, *, dry_run: bool = False
) -> list[LeadState]:
    """
    Ask this workspace's discovery provider about one (niche, region) pair.

    No dedupe yet -- `discover()` does that across the whole batch, because a
    duplicate can only be recognised against the other targets.

    There is no branch on which vendor to use. The niche's `type` decides which
    capability applies and the resolver decides who serves it, so a workspace
    that switches from a contact database to its own CSV exports changes one
    line of YAML and this function does not notice.
    """
    niche = config.niche(target["niche_id"])
    discovery = niche.get("discovery") or {}
    region = target["region"]
    locations = (discovery.get("locations") or {}).get(region) or []
    limit = int(discovery.get("max_results_per_location", 20))

    if dry_run:
        return load_fixture_leads(config.tenant_id, [target])

    kind = str(niche.get("type") or "local_business")
    provider = discovery_for(config, niche, tenant_id=config.tenant_id)

    request = DiscoveryRequest(
        kind=kind,
        search_terms=list(discovery.get("search_terms") or []),
        locations=list(locations),
        titles=list(discovery.get("titles") or []),
        industries=list(discovery.get("industries") or []),
        employee_range=discovery.get("employee_range"),
        limit=limit,
        csv_glob=str(discovery.get("csv_import_glob", "") or ""),
        niche_id=target["niche_id"],
        region=region,
    )

    leads: list[LeadState] = []
    for found in provider.find(request):
        if not found.company_name:
            continue
        if not found.is_open:
            # A permanently closed business is not a lead. Dropped here rather
            # than at qualification so it never costs a model call.
            continue
        leads.append(_to_lead(found, target, config.tenant_id, dry_run, kind))

    log.info(
        "discovery tenant=%s niche=%s region=%s provider=%s -> %d",
        config.tenant_id, target["niche_id"], region,
        getattr(provider, "id", "?"), len(leads),
    )
    return leads


def deduplicate(
    tenant_id: str, leads: list[LeadState], *, seen: dict[str, Any] | None = None
) -> tuple[list[LeadState], int]:
    """
    Drop leads this tenant has already discovered, and collapse duplicates
    inside this batch. Returns (fresh_leads, dropped_count).

    Matching is by domain when a website is known, else normalised name +
    location -- the same key `make_dedupe_key` builds, so the ledger and the
    lead_id can never disagree.
    """
    seen = load_seen(tenant_id) if seen is None else seen
    fresh: list[LeadState] = []
    batch_keys: set[str] = set()
    dropped = 0

    for lead in leads:
        key = lead.get("dedupe_key") or make_dedupe_key(
            lead.get("company_name", ""), lead.get("location", ""), lead.get("website", "")
        )
        if key in seen or key in batch_keys:
            dropped += 1
            log.debug("dedupe: dropping %r (key=%s)", lead.get("company_name"), key)
            continue
        batch_keys.add(key)
        lead["dedupe_key"] = key
        lead["lead_id"] = make_lead_id(tenant_id, key)
        fresh.append(lead)

    return fresh, dropped


def discover(
    config: TenantConfig,
    targets: list[BatchTarget],
    *,
    dry_run: bool = False,
    record: bool = True,
) -> tuple[list[LeadState], list[BatchTarget]]:
    """
    Discover across every target, deduplicate, and report low-yield targets.

    Returns (leads, low_yield_targets). A target is low-yield when the number of
    NEW leads it produced falls below `min_expected_leads * low_yield_threshold_ratio`.
    Using new-after-dedupe rather than raw results is deliberate: a region that
    returns 20 companies you already contacted last month IS low yield, and the
    raw count would hide that.
    """
    tenant_id = config.tenant_id
    seen = load_seen(tenant_id)
    ratio = float(config.runtime.get("low_yield_threshold_ratio", 0.5))

    all_leads: list[LeadState] = []
    low_yield: list[BatchTarget] = []
    newly_seen: dict[str, Any] = {}

    for target in targets:
        raw = discover_for_target(config, target, dry_run=dry_run)
        fresh, dropped = deduplicate(tenant_id, raw, seen=seen)

        for lead in fresh:
            seen[lead["dedupe_key"]] = {
                "company_name": lead.get("company_name", ""),
                "first_seen": lead["discovered_at"].isoformat(),
                "niche_id": lead.get("niche_id", ""),
                "region": lead.get("region", ""),
            }
            newly_seen[lead["dedupe_key"]] = seen[lead["dedupe_key"]]

        niche = config.niche(target["niche_id"])
        expected = int((niche.get("discovery") or {}).get("min_expected_leads", 5))
        threshold = max(1, int(expected * ratio))
        if len(fresh) < threshold:
            low_yield.append(target)
            log.warning(
                "low_yield tenant=%s niche=%s region=%s new=%d expected>=%d "
                "(raw=%d, deduped_out=%d)",
                tenant_id, target["niche_id"], target["region"],
                len(fresh), threshold, len(raw), dropped,
            )
        else:
            log.info(
                "discovery tenant=%s niche=%s region=%s new=%d (raw=%d, deduped_out=%d)",
                tenant_id, target["niche_id"], target["region"],
                len(fresh), len(raw), dropped,
            )
        all_leads.extend(fresh)

    # The ledger is only written for a real run. A dry run must not poison the
    # dedupe history and make tomorrow's live batch skip real leads.
    if record and not dry_run:
        remember_seen(tenant_id, newly_seen)

    capped = all_leads[: config.max_leads_per_run]
    if len(all_leads) > len(capped):
        log.info(
            "capping batch at max_leads_per_run=%d (discovered %d)",
            config.max_leads_per_run, len(all_leads),
        )
    return capped, low_yield


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n1_discovery")
def n1_discovery(state: LeadState) -> dict:
    """
    In-graph discovery step for a single, already-seeded lead.

    Batch fan-out happened in `discover()` before the graph was entered, so
    this node's remaining jobs are the per-lead ones: confirm the dedupe key
    and lead_id are consistent, and reject a lead that arrived with no
    identifying information at all.
    """
    company = (state.get("company_name") or "").strip()
    if not company:
        raise SkipLead(
            archived=True,
            archive_reason="no_company_name",
            next_action="dropped at discovery: no company name",
        )

    key = make_dedupe_key(
        company, state.get("location", ""), state.get("website", "")
    )
    updates: dict[str, Any] = {}
    if state.get("dedupe_key") != key:
        updates["dedupe_key"] = key
        updates["lead_id"] = make_lead_id(state["tenant_id"], key)

    # Normalise the website early so every later node -- enrichment, Hunter,
    # the compliance domain check -- reasons about the same string.
    website = state.get("website") or ""
    if website and not website.startswith(("http://", "https://")):
        domain = normalise_domain(website)
        if domain:
            updates["website"] = f"https://{domain}"

    return updates
