"""
N1 discovery and N2 enrichment tests (Build step 3).

No test here touches a live network service: Places, Apollo, Hunter and the
scraper are all mocked or driven from the dry-run fixture.
"""

from __future__ import annotations

import json

import pytest

from src.counters import Counter
from src.integrations import apollo, hunter, places, scraping
from src.nodes.n0_config_load import load_tenant_config, resolve_batch_targets
from src.nodes.n1_discovery import (
    deduplicate,
    discover,
    forget_all,
    load_fixture_leads,
    load_seen,
    n1_discovery,
    remember_seen,
)
from src.nodes.n2_enrichment import (
    detect_b2b_signals,
    detect_local_signals,
    match_icp_signals,
    n2_enrichment,
    select_hunter_candidates,
)
from src.state import BatchTarget, make_dedupe_key, new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Give every test its own .data directory so ledgers cannot leak."""
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


# --------------------------------------------------------------------------- #
# Fixture-driven discovery (dry run)
# --------------------------------------------------------------------------- #

def test_fixture_leads_load_for_every_target(config):
    leads = load_fixture_leads(EXAMPLE)
    assert len(leads) == 9
    assert all(lead["tenant_id"] == EXAMPLE for lead in leads)
    assert all(lead["dry_run"] for lead in leads)


def test_fixture_leads_filter_by_target():
    target = BatchTarget(niche_id="local_dental", region="UK", language="en")
    leads = load_fixture_leads(EXAMPLE, [target])
    assert leads
    assert {lead["region"] for lead in leads} == {"UK"}
    assert {lead["niche_id"] for lead in leads} == {"local_dental"}


def test_french_fixture_is_present_and_tagged_eu():
    leads = load_fixture_leads(EXAMPLE)
    french = [lead for lead in leads if lead["language"] == "fr"]
    assert len(french) == 1
    assert french[0]["region"] == "EU"
    assert french[0]["company_name"] == "Atelier Coiffure Saint-Germain"


def test_dry_run_discover_does_not_write_the_dedupe_ledger(config):
    forget_all(EXAMPLE)
    targets = resolve_batch_targets(config)
    leads, _ = discover(config, targets, dry_run=True)
    assert leads
    assert load_seen(EXAMPLE) == {}, (
        "a dry run must not poison the ledger and make tomorrow's live batch "
        "skip real leads"
    )


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #

def test_dedupe_collapses_the_fixture_duplicate():
    """
    The fixture deliberately contains Bright Smile twice with different casing,
    a trailing path and a slightly different name.
    """
    leads = load_fixture_leads(EXAMPLE)
    bright = [lead for lead in leads if "bright" in lead["company_name"].lower()]
    assert len(bright) == 2
    fresh, dropped = deduplicate(EXAMPLE, bright, seen={})
    assert len(fresh) == 1
    assert dropped == 1


def test_dedupe_respects_the_persisted_ledger():
    leads = load_fixture_leads(EXAMPLE)[:3]
    fresh, dropped = deduplicate(EXAMPLE, leads, seen={})
    assert len(fresh) == 3 and dropped == 0

    seen = {lead["dedupe_key"]: {"company_name": lead["company_name"]} for lead in fresh}
    again, dropped_again = deduplicate(EXAMPLE, load_fixture_leads(EXAMPLE)[:3], seen=seen)
    assert again == [] and dropped_again == 3


def test_dedupe_ledger_is_per_tenant(tmp_path):
    """Tenant A having seen a company must not hide it from tenant B."""
    lead_a = new_lead_state(
        tenant_id="tenant_a", niche_id="local_dental", region="UK",
        company_name="Acme Dental", website="https://acme-dental.co.uk",
    )
    remember_seen("tenant_a", {lead_a["dedupe_key"]: {"company_name": "Acme Dental"}})

    lead_b = new_lead_state(
        tenant_id="tenant_b", niche_id="local_dental", region="UK",
        company_name="Acme Dental", website="https://acme-dental.co.uk",
    )
    fresh, dropped = deduplicate("tenant_b", [lead_b])
    assert len(fresh) == 1 and dropped == 0
    assert load_seen("tenant_a") != {}
    assert load_seen("tenant_b") == {}


def test_dedupe_assigns_a_deterministic_lead_id():
    lead = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Acme", website="https://acme.co.uk",
    )
    fresh, _ = deduplicate(EXAMPLE, [lead], seen={})
    key = make_dedupe_key("Acme", "", "https://acme.co.uk")
    assert fresh[0]["dedupe_key"] == key
    assert fresh[0]["lead_id"].startswith(f"{EXAMPLE}-")


# --------------------------------------------------------------------------- #
# Low yield
# --------------------------------------------------------------------------- #

def test_low_yield_is_flagged_not_raised(config, monkeypatch):
    """A thin region is information, not a batch failure."""
    monkeypatch.setattr(
        "src.nodes.n1_discovery.discover_for_target",
        lambda cfg, target, dry_run=False: [
            new_lead_state(
                tenant_id=cfg.tenant_id, niche_id=target["niche_id"],
                region=target["region"], company_name="Only One",
                website="https://only-one.example",
            )
        ],
    )
    targets = resolve_batch_targets(config)
    leads, low_yield = discover(config, targets, dry_run=False, record=False)
    assert leads                      # the batch still produced work
    assert len(low_yield) == len(targets)


def test_good_yield_is_not_flagged(config, monkeypatch):
    def many(cfg, target, dry_run=False):
        return [
            new_lead_state(
                tenant_id=cfg.tenant_id, niche_id=target["niche_id"],
                region=target["region"], company_name=f"Co {i}",
                website=f"https://co{i}-{target['region']}.example",
            )
            for i in range(10)
        ]

    monkeypatch.setattr("src.nodes.n1_discovery.discover_for_target", many)
    targets = resolve_batch_targets(config)[:1]
    _, low_yield = discover(config, targets, dry_run=False, record=False)
    assert low_yield == []


def test_max_leads_per_run_caps_the_batch(config, monkeypatch):
    def flood(cfg, target, dry_run=False):
        return [
            new_lead_state(
                tenant_id=cfg.tenant_id, niche_id=target["niche_id"],
                region=target["region"], company_name=f"Co {target['region']} {i}",
                website=f"https://co{i}-{target['region']}.example",
            )
            for i in range(50)
        ]

    monkeypatch.setattr("src.nodes.n1_discovery.discover_for_target", flood)
    leads, _ = discover(config, resolve_batch_targets(config), record=False)
    assert len(leads) == config.max_leads_per_run


# --------------------------------------------------------------------------- #
# Provider adapters (mocked)
# --------------------------------------------------------------------------- #

def test_local_discovery_uses_places(config, monkeypatch):
    monkeypatch.setattr(
        places, "search",
        lambda query, limit=20: [
            places.PlaceResult(
                name="Bright Smile", address="Manchester", website="https://bs.co.uk",
                phone="0161 555 0100", rating=4.6, review_count=34, source="google_places",
            )
        ],
    )
    from src.nodes.n1_discovery import discover_for_target

    target = BatchTarget(niche_id="local_dental", region="UK", language="en")
    leads = discover_for_target(config, target)
    assert leads
    assert leads[0]["company_name"] == "Bright Smile"
    assert leads[0]["source"] == "google_places"
    assert any("34 reviews" in s for s in leads[0]["signals"])


def test_permanently_closed_places_are_dropped(config, monkeypatch):
    monkeypatch.setattr(
        places, "search",
        lambda query, limit=20: [
            places.PlaceResult(name="Gone", business_status="CLOSED_PERMANENTLY")
        ],
    )
    from src.nodes.n1_discovery import discover_for_target

    target = BatchTarget(niche_id="local_dental", region="UK", language="en")
    assert discover_for_target(config, target) == []


def test_b2b_discovery_uses_apollo(config, monkeypatch):
    monkeypatch.setattr(
        apollo, "search",
        lambda **kwargs: [
            apollo.ContactResult(
                company_name="Maple Route", contact_name="Kieran Moreau",
                title="Head of CS", linkedin_url="https://linkedin.com/in/kieran",
                employee_count=80, source="apollo",
            )
        ],
    )
    from src.nodes.n1_discovery import discover_for_target

    target = BatchTarget(niche_id="b2b_saas_ops", region="CA", language="en")
    leads = discover_for_target(config, target)
    assert leads[0]["contact_name"] == "Kieran Moreau"
    assert any("decision-maker" in s for s in leads[0]["signals"])


def test_apollo_csv_import_handles_sales_navigator_headers(tmp_path, monkeypatch):
    csv_path = tmp_path / "b2b_saas_ops_export.csv"
    csv_path.write_text(
        "First Name,Last Name,Company,Job Title,Person Linkedin Url,Email\n"
        "Katrin,Vogel,Nordwind Logistics,VP Customer Experience,"
        "https://linkedin.com/in/kvogel,\n"
        "No,Contact,Ghost Ltd,CTO,,\n",
        encoding="utf-8",
    )
    results = apollo.read_csv(csv_path)
    assert len(results) == 1, "the row with neither email nor LinkedIn must be dropped"
    assert results[0].contact_name == "Katrin Vogel"
    assert results[0].company_name == "Nordwind Logistics"
    assert results[0].source == "csv"


# --------------------------------------------------------------------------- #
# N1 node
# --------------------------------------------------------------------------- #

def test_n1_node_archives_a_lead_with_no_company_name():
    state = new_lead_state(tenant_id=EXAMPLE, niche_id="local_dental", region="UK")
    update = n1_discovery(state)
    assert update["archived"] is True
    assert update["archive_reason"] == "no_company_name"


def test_n1_node_normalises_a_bare_domain():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Acme", website="acme.co.uk",
    )
    assert n1_discovery(state)["website"] == "https://acme.co.uk"


# --------------------------------------------------------------------------- #
# Signal detection
# --------------------------------------------------------------------------- #

def test_local_signals_detect_a_missing_booking_system():
    html = "<html><head></head><body>Call us on 0161 555 0100</body></html>"
    signals = detect_local_signals(html, "Call us on 0161 555 0100")
    assert any("no online booking" in s for s in signals)
    assert any("live chat" in s for s in signals)
    assert any("viewport" in s for s in signals)


def test_local_signals_stay_quiet_when_booking_exists():
    html = (
        '<html><head><meta name="viewport" content="width=device-width"></head>'
        '<body><a href="tel:01615550100">Call</a>'
        '<a href="https://calendly.com/x">Book online</a>'
        '<script src="https://widget.intercom.io/x"></script></body></html>'
    )
    signals = detect_local_signals(html, "Book online")
    assert not any("no online booking" in s for s in signals)
    assert not any("live chat" in s for s in signals)


def test_b2b_signals_detect_hiring_and_stack():
    html = "<html><body>Join our team! Powered by Zendesk. We raised a seed round.</body></html>"
    signals = detect_b2b_signals(html, "join our team zendesk seed round")
    assert any("hiring" in s for s in signals)
    assert any("helpdesk" in s for s in signals)
    assert any("seed" in s for s in signals)


def test_icp_signals_outrank_generic_detectors():
    icp = {"good_signals": ["no online booking system", "unanswered Google reviews"]}
    hits = match_icp_signals("our site has no online booking system at present", icp)
    assert "no online booking system" in hits


# --------------------------------------------------------------------------- #
# Email extraction and ranking
# --------------------------------------------------------------------------- #

def test_extract_emails_prefers_on_domain_role_based():
    html = """
      <a href="mailto:someone@gmail.com">personal</a>
      <a href="mailto:hello@acme.co.uk">hello</a>
      <a href="mailto:jane.doe@acme.co.uk">jane</a>
      <img src="logo@2x.png">
    """
    found = scraping.extract_emails(html, prefer_domain="acme.co.uk")
    assert found[0] == "hello@acme.co.uk"
    assert "logo@2x.png" not in found


def test_role_based_detection():
    assert scraping.is_role_based("info@acme.com")
    assert scraping.is_role_based("frontdesk@acme.com")
    assert not scraping.is_role_based("jane.doe@acme.com")


def test_robots_disallow_is_honoured(monkeypatch):
    monkeypatch.setattr(scraping, "is_allowed", lambda url: False)
    page = scraping.fetch("https://acme.co.uk/contact", delay=0)
    assert page.error == "robots_disallowed"
    assert not page.ok


# --------------------------------------------------------------------------- #
# Hunter budgeting -- the 25/month cap
# --------------------------------------------------------------------------- #

def _leads(n: int, **overrides) -> list:
    out = []
    for i in range(n):
        lead = new_lead_state(
            tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
            company_name=f"Co {i}", website=f"https://co{i}.example",
        )
        lead.update(overrides)
        out.append(lead)
    return out


def test_hunter_candidates_respect_tenant_top_n(monkeypatch):
    monkeypatch.setattr(hunter, "remaining_lookups", lambda: 25)
    chosen = select_hunter_candidates(_leads(20), top_n=5)
    assert len(chosen) == 5


def test_hunter_candidates_respect_the_remaining_monthly_budget(monkeypatch):
    """Asking for 5 when 2 remain must yield 2, not 5."""
    monkeypatch.setattr(hunter, "remaining_lookups", lambda: 2)
    assert len(select_hunter_candidates(_leads(20), top_n=5)) == 2


def test_hunter_candidates_are_empty_when_quota_is_spent(monkeypatch):
    monkeypatch.setattr(hunter, "remaining_lookups", lambda: 0)
    monkeypatch.setattr(hunter, "quota_status", lambda: "spent")
    assert select_hunter_candidates(_leads(20), top_n=5) == []


def test_leads_that_already_have_an_email_are_not_candidates(monkeypatch):
    monkeypatch.setattr(hunter, "remaining_lookups", lambda: 25)
    leads = _leads(5, contact_email="hello@co.example")
    assert select_hunter_candidates(leads, top_n=5) == []


def test_hunter_prioritises_leads_with_no_other_contact_route(monkeypatch):
    monkeypatch.setattr(hunter, "remaining_lookups", lambda: 1)
    reachable = _leads(1)[0]
    reachable["linkedin_url"] = "https://linkedin.com/company/x"
    reachable["company_name"] = "Has LinkedIn"
    unreachable = _leads(1)[0]
    unreachable["company_name"] = "No route"
    unreachable["website"] = "https://no-route.example"
    unreachable["lead_id"] = "target"
    chosen = select_hunter_candidates([reachable, unreachable], top_n=1)
    assert chosen == ["target"]


def test_hunter_counter_hard_stops_at_the_free_plan_cap(monkeypatch):
    """The 25/month ceiling is enforced by the durable counter, not a comment."""
    counter = Counter("hunter_lookups", cap=25, period="month")
    counter.reset()
    for _ in range(25):
        counter.check(1)
        counter.consume(1)
    assert counter.remaining == 0
    assert counter.would_exceed(1)
    with pytest.raises(Exception, match="quota"):
        counter.check(1)


def test_hunter_find_email_returns_none_when_quota_is_spent(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    counter = Counter("hunter_lookups", cap=25, period="month")
    counter.reset()
    counter.consume(25)

    def explode(*args, **kwargs):
        raise AssertionError("network must not be touched when the quota is spent")

    monkeypatch.setattr(hunter, "_domain_search", explode)
    monkeypatch.setattr(hunter, "_email_finder", explode)
    assert hunter.find_email("acme.co.uk") is None


def test_hunter_low_confidence_result_is_discarded(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    Counter("hunter_lookups", cap=25, period="month").reset()
    monkeypatch.setattr(
        hunter, "_domain_search",
        lambda domain: hunter.HunterResult(email="guess@acme.co.uk", confidence=40),
    )
    assert hunter.find_email("acme.co.uk") is None


def test_hunter_good_result_is_returned_and_counted(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake-key")
    counter = Counter("hunter_lookups", cap=25, period="month")
    counter.reset()
    monkeypatch.setattr(
        hunter, "_domain_search",
        lambda domain: hunter.HunterResult(
            email="priya@acme.co.uk", confidence=92,
            first_name="Priya", last_name="Raman", position="Practice Manager",
        ),
    )
    result = hunter.find_email("acme.co.uk")
    assert result and result.email == "priya@acme.co.uk"
    assert counter.used == 1


# --------------------------------------------------------------------------- #
# N2 node
# --------------------------------------------------------------------------- #

def test_n2_marks_a_lead_with_no_contact_route_unreachable(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_salon", region="AU",
        company_name="Ivy & Oak", dry_run=True,
    )
    update = n2_enrichment(state, tenant_config=config)
    assert update["unreachable"] is True
    assert update["archived"] is True
    assert update["archive_reason"] == "unreachable"


def test_n2_keeps_a_lead_reachable_by_linkedin_alone(config):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="EU",
        company_name="Nordwind", linkedin_url="https://linkedin.com/company/nordwind",
        dry_run=True,
    )
    state["signals"] = ["three open Customer Success roles"]
    update = n2_enrichment(state, tenant_config=config)
    assert update["unreachable"] is False
    assert not update.get("archived")


def test_n2_does_not_touch_the_network_in_dry_run(config, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dry run must not make a network call")

    monkeypatch.setattr(scraping, "fetch", explode)
    monkeypatch.setattr(scraping, "fetch_rendered", explode)
    monkeypatch.setattr(hunter, "find_email", explode)

    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile", website="https://bright.co.uk",
        contact_email="hello@bright.co.uk", dry_run=True,
    )
    state["signals"] = ["no online booking"]
    update = n2_enrichment(state, tenant_config=config)
    assert update["signals"] == ["no online booking"]


def test_n2_scrapes_and_merges_signals(config, monkeypatch):
    html = "<html><body>Call 0161 555 0100 <a href='mailto:hello@bright.co.uk'>x</a></body></html>"
    monkeypatch.setattr(
        scraping, "fetch",
        lambda url, delay=0.5: scraping.PageFetch(url=url, status=200, html=html),
    )
    monkeypatch.setattr(
        scraping, "fetch_rendered",
        lambda url, wait_ms=2500: scraping.PageFetch(url=url, status=200, html=html),
    )
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile", website="https://bright.co.uk",
    )
    update = n2_enrichment(state, tenant_config=config)
    assert update["contact_email"] == "hello@bright.co.uk"
    assert any("no online booking" in s for s in update["signals"])
    assert update["unreachable"] is False


def test_n2_failure_flags_manual_review_rather_than_raising(config, monkeypatch):
    """Section 8: one lead's node failure must not crash the batch."""
    def explode(*args, **kwargs):
        raise RuntimeError("scraper exploded")

    monkeypatch.setattr("src.nodes.n2_enrichment.scrape_lead", explode)
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
        company_name="Bright Smile", website="https://bright.co.uk",
    )
    update = n2_enrichment(state, tenant_config=config)
    assert update["needs_manual_review"] is True
    assert "n2_enrichment" in update["manual_review_reason"]
    assert update["errors"][-1]["node"] == "n2_enrichment"
