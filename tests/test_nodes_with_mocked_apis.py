"""
Node tests against mocked external APIs (Build step 10).

Every external call (LLM, Places, Apollo, Hunter, email send and read, CRM) is
mocked here. No test in this suite may touch a live network service -- the first
test in the file enforces that by making the socket module refuse to connect.

This file covers the integration LAYER: provider fallback, quota guards, error
degradation and the free-tier ceilings. Per-node behaviour lives in the
node-specific test modules.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest

from src.counters import Counter, QuotaExceeded
from src.integrations import (
    apollo,
    email_reader,
    email_sender,
    hunter,
    llm,
    places,
    scraping,
    sheets_crm,
    translation,
)
from src.nodes.n0_config_load import load_tenant_config
from src.reliability import retry_once
from src.state import CRM_COLUMNS, new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _no_network(tmp_path, monkeypatch):
    """
    Hard guarantee: a test that tries to open a socket fails loudly rather than
    quietly hitting a real API on someone's free-tier quota.
    """
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "checkpoints"))

    def blocked(*args, **kwargs):
        raise AssertionError(
            "a test tried to open a network connection; mock the integration instead"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


# =========================================================================== #
# The no-network guard itself
# =========================================================================== #

def test_the_network_guard_works():
    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("example.com", 80))


# =========================================================================== #
# LLM provider chain
# =========================================================================== #

def test_the_chain_falls_through_to_the_next_provider(monkeypatch):
    attempted: list[str] = []

    monkeypatch.setattr(llm, "provider_available", lambda name: name in ("groq", "gemini", "mock"))
    monkeypatch.setattr(llm, "_build_client", lambda provider, temp: _FailingClient(provider, attempted))
    llm.register_mock("test_task", lambda prompt, ctx: "mock answer")

    response = llm.complete("hello", task="test_task")
    assert response.provider == "mock"
    assert response.text == "mock answer"
    assert attempted == ["groq", "groq", "gemini", "gemini"], (
        "each provider should be retried once before the chain moves on"
    )


class _FailingClient:
    def __init__(self, provider: str, log: list[str]):
        self.provider, self.log = provider, log

    def invoke(self, messages):
        self.log.append(self.provider)
        raise RuntimeError(f"{self.provider} is down")


def test_a_working_provider_short_circuits_the_chain(monkeypatch):
    monkeypatch.setattr(llm, "provider_available", lambda name: True)

    class Working:
        def invoke(self, messages):
            return type("R", (), {"content": "real answer"})()

    monkeypatch.setattr(llm, "_build_client", lambda provider, temp: Working())
    response = llm.complete("hello", task="test_task", provider="groq")
    assert response.provider == "groq"
    assert response.text == "real answer"


def test_llm_unavailable_when_every_provider_fails_and_mock_is_off(monkeypatch):
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "groq")
    monkeypatch.setattr(
        llm, "_build_client", lambda provider, temp: _FailingClient(provider, [])
    )
    with pytest.raises(llm.LLMUnavailable):
        llm.complete("hello", task="test_task", allow_mock=False)


def test_content_block_responses_are_flattened(monkeypatch):
    """Some providers return a list of content blocks rather than a string."""
    monkeypatch.setattr(llm, "provider_available", lambda name: True)

    class Blocks:
        def invoke(self, messages):
            return type("R", (), {"content": [{"text": "part one "}, {"text": "part two"}]})()

    monkeypatch.setattr(llm, "_build_client", lambda provider, temp: Blocks())
    assert llm.complete("x", task="test_task", provider="groq").text == "part one part two"


def test_an_empty_response_is_treated_as_a_failure(monkeypatch):
    monkeypatch.setattr(llm, "provider_available", lambda name: name in ("groq", "mock"))

    class Empty:
        def invoke(self, messages):
            return type("R", (), {"content": "   "})()

    monkeypatch.setattr(llm, "_build_client", lambda provider, temp: Empty())
    llm.register_mock("test_task", lambda prompt, ctx: "fallback")
    assert llm.complete("x", task="test_task").provider == "mock"


def test_a_missing_mock_handler_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    with pytest.raises(llm.LLMUnavailable, match="no mock handler"):
        llm.complete("x", task="a_task_nobody_registered")


# =========================================================================== #
# Retry-once semantics (Section 8)
# =========================================================================== #

def test_retry_once_retries_exactly_once():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    assert retry_once(flaky, _backoff_seconds=0) == "ok"
    assert calls["n"] == 2


def test_retry_once_does_not_retry_twice():
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError):
        retry_once(always_fails, _backoff_seconds=0)
    assert calls["n"] == 2, "retry-once means two attempts, never three"


# =========================================================================== #
# Free-tier counters
# =========================================================================== #

def test_a_counter_persists_across_instances():
    Counter("demo", cap=10, period="month").consume(4)
    assert Counter("demo", cap=10, period="month").used == 4


def test_a_counter_resets_when_the_period_rolls_over(monkeypatch):
    from src.nodes import n0_config_load  # noqa: F401  (import for symmetry)

    counter = Counter("rollover", cap=10, period="month")
    counter.consume(9)
    assert counter.remaining == 1

    # Pretend the calendar month advanced.
    import src.counters as counters_module

    real_period_key = counters_module._period_key
    monkeypatch.setattr(
        counters_module, "_period_key",
        lambda period, now=None: "1999-01" if period == "month" else real_period_key(period, now),
    )
    assert Counter("rollover", cap=10, period="month").used == 0


def test_a_counter_write_is_atomic(tmp_path):
    """A process killed mid-write must not corrupt a quota file to zero."""
    counter = Counter("atomic", cap=10, period="month")
    counter.consume(3)
    text = counter.path.read_text(encoding="utf-8")
    import json

    assert json.loads(text)["count"] == 3
    assert not list(counter.path.parent.glob("*.tmp")), "no temp file left behind"


@pytest.mark.parametrize(
    "name,cap",
    [("hunter_lookups", 25), ("deepl_chars", 500_000), ("places_requests", 2000)],
)
def test_documented_free_tier_ceilings(name, cap):
    from src.counters import deepl_counter, hunter_counter, places_counter

    counters = {
        "hunter_lookups": hunter_counter, "deepl_chars": deepl_counter,
        "places_requests": places_counter,
    }
    assert counters[name]().cap == cap


# =========================================================================== #
# Places
# =========================================================================== #

def test_places_falls_back_to_osm_without_a_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    called = {"osm": False}

    def fake_osm(query, limit):
        called["osm"] = True
        return [places.PlaceResult(name="Osm Co", source="osm")]

    monkeypatch.setattr(places, "_search_nominatim", fake_osm)
    results = places.search("dental clinic in Manchester")
    assert called["osm"]
    assert results[0].source == "osm"


def test_places_falls_back_to_osm_when_the_quota_is_spent(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "fake")
    Counter("places_requests", cap=2000, period="month").consume(2000)

    def explode(query, limit):
        raise AssertionError("Places must not be called with the quota spent")

    monkeypatch.setattr(places, "_search_google", explode)
    monkeypatch.setattr(
        places, "_search_nominatim",
        lambda query, limit: [places.PlaceResult(name="Osm Co", source="osm")],
    )
    assert places.search("x")[0].source == "osm"


def test_places_returns_empty_when_both_providers_fail(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)

    def explode(query, limit):
        raise RuntimeError("nominatim down")

    monkeypatch.setattr(places, "_search_nominatim", explode)
    assert places.search("x") == []


def test_places_query_building():
    assert places.build_query("dental clinic", "Manchester") == "dental clinic in Manchester"


def test_permanently_closed_detection():
    assert not places.PlaceResult(name="X", business_status="CLOSED_PERMANENTLY").is_open
    assert places.PlaceResult(name="X", business_status="OPERATIONAL").is_open


# =========================================================================== #
# Apollo
# =========================================================================== #

def test_apollo_falls_back_to_csv_when_the_api_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("APOLLO_API_KEY", "fake")
    monkeypatch.setenv("APOLLO_CSV_IMPORT_DIR", str(tmp_path))
    (tmp_path / "b2b_x.csv").write_text(
        "Company,Name,Email\nNordwind,Katrin Vogel,katrin@nordwind.de\n", encoding="utf-8"
    )

    def explode(*args, **kwargs):
        raise RuntimeError("apollo 403")

    monkeypatch.setattr(apollo, "_search_apollo", explode)
    results = apollo.search(titles=["Head of CS"], csv_glob="b2b_*.csv")
    assert len(results) == 1
    assert results[0].source == "csv"


def test_apollo_redacted_emails_are_dropped(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "fake")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"people": [{
                "name": "Katrin Vogel", "title": "VP CX",
                "email": "email_not_unlocked@domain.com",
                "organization": {"name": "Nordwind", "website_url": "https://nordwind.de"},
            }]}

    monkeypatch.setattr(apollo.requests, "post", lambda *a, **k: Response())
    results = apollo._search_apollo(["VP CX"], [], [], None, 10)
    assert results[0].email == "", "a locked Apollo email must not be used as an address"


def test_csv_headers_are_matched_flexibly(tmp_path):
    (tmp_path / "x.csv").write_text(
        "Organization Name,Full Name,Job Title,Person Linkedin Url,Work Email,# Employees\n"
        "Nordwind GmbH,Katrin Vogel,VP CX,https://linkedin.com/in/kv,kv@nordwind.de,120\n",
        encoding="utf-8",
    )
    result = apollo.read_csv(tmp_path / "x.csv")[0]
    assert result.company_name == "Nordwind GmbH"
    assert result.contact_name == "Katrin Vogel"
    assert result.employee_count == 120


def test_a_csv_with_no_company_column_is_rejected(tmp_path):
    (tmp_path / "x.csv").write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    assert apollo.read_csv(tmp_path / "x.csv") == []


# =========================================================================== #
# Scraping
# =========================================================================== #

def test_a_transport_failure_is_data_not_an_exception(monkeypatch):
    """A dead website is ordinary information about a lead."""
    monkeypatch.setattr(scraping, "is_allowed", lambda url: True)

    def explode(*args, **kwargs):
        raise scraping.requests.ConnectionError("dns failure")

    monkeypatch.setattr(scraping.requests, "get", explode)
    page = scraping.fetch("https://dead.example", delay=0)
    assert not page.ok
    assert "ConnectionError" in page.error


def test_non_html_content_is_skipped(monkeypatch):
    monkeypatch.setattr(scraping, "is_allowed", lambda url: True)

    class Response:
        status_code = 200
        headers = {"Content-Type": "application/pdf"}
        text = "%PDF-1.4"
        url = "https://x.example/a.pdf"

    monkeypatch.setattr(scraping.requests, "get", lambda *a, **k: Response())
    page = scraping.fetch("https://x.example/a.pdf", delay=0)
    assert "non-html" in page.error


def test_playwright_absence_degrades_to_a_static_fetch(monkeypatch):
    monkeypatch.setattr(scraping, "is_allowed", lambda url: True)
    monkeypatch.setattr(
        scraping, "fetch",
        lambda url, delay=0.5: scraping.PageFetch(url=url, status=200, html="<html>static</html>"),
    )

    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    page = scraping.fetch_rendered("https://x.example")
    assert page.ok
    assert "static" in page.html


def test_visible_text_strips_scripts():
    html = "<html><body><script>var x=1</script><p>Hello there</p></body></html>"
    text = scraping.visible_text(html)
    assert "Hello there" in text
    assert "var x" not in text


def test_candidate_pages_are_capped(monkeypatch):
    monkeypatch.setenv("SCRAPER_MAX_PAGES_PER_SITE", "3")
    assert len(scraping.candidate_pages("https://x.example")) == 3


# =========================================================================== #
# Hunter
# =========================================================================== #

def test_hunter_without_a_key_returns_none(monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    assert hunter.find_email("acme.co.uk") is None


def test_a_failed_hunter_call_still_counts_against_the_quota(monkeypatch):
    """
    A call that errored may still have been billed. Counting it is the
    conservative choice against a 25/month ceiling.
    """
    monkeypatch.setenv("HUNTER_API_KEY", "fake")
    counter = hunter.hunter_counter()
    counter.reset()

    def explode(domain):
        raise RuntimeError("hunter 500")

    monkeypatch.setattr(hunter, "_domain_search", explode)
    assert hunter.find_email("acme.co.uk") is None
    assert counter.used == 1


def test_hunter_prefers_a_decision_maker(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "fake")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"emails": [
                {"value": "info@acme.com", "confidence": 95, "seniority": "junior",
                 "department": "support"},
                {"value": "ceo@acme.com", "confidence": 90, "seniority": "executive",
                 "department": "management", "first_name": "Ada", "last_name": "Byron",
                 "position": "CEO"},
            ]}}

    monkeypatch.setattr(hunter.requests, "get", lambda *a, **k: Response())
    hunter.hunter_counter().reset()
    result = hunter.find_email("acme.com")
    assert result.email == "ceo@acme.com"
    assert result.full_name == "Ada Byron"


# =========================================================================== #
# Translation
# =========================================================================== #

def test_deepl_quota_exhaustion_falls_back_to_the_llm(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "fake")
    Counter("deepl_chars", cap=500_000, period="month").consume(500_000)

    def explode(text, target):
        raise AssertionError("DeepL must not be called with the budget spent")

    monkeypatch.setattr(translation, "_translate_deepl", explode)
    monkeypatch.setattr(translation, "_translate_llm", lambda text, lang: "traduit")
    result = translation.translate("hello", "fr")
    assert result.provider == "llm"
    assert result.text == "traduit"


def test_deepl_consumes_characters_on_success(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "fake")
    counter = Counter("deepl_chars", cap=500_000, period="month")
    counter.reset()
    monkeypatch.setattr(translation, "_translate_deepl", lambda text, target: "bonjour")
    translation.translate("hello", "fr")
    assert counter.used == len("hello")


def test_arabic_skips_deepl_because_it_has_no_target(monkeypatch):
    monkeypatch.setenv("DEEPL_API_KEY", "fake")

    def explode(text, target):
        raise AssertionError("DeepL has no Arabic target")

    monkeypatch.setattr(translation, "_translate_deepl", explode)
    monkeypatch.setattr(translation, "_translate_llm", lambda text, lang: "مرحبا")
    assert translation.translate("hello", "ar").provider == "llm"


def test_batched_field_translation_keeps_fields_paired(monkeypatch):
    monkeypatch.setattr(
        translation, "translate",
        lambda text, language: translation.Translation(
            text=text.replace("hello", "bonjour"), language=language,
            provider="deepl", translated=True,
        ),
    )
    fields = {"subject": "hello subject", "body": "hello body"}
    out, translated, provider = translation.translate_fields(fields, "fr")
    assert translated
    assert out["subject"] == "bonjour subject"
    assert out["body"] == "bonjour body"


def test_a_mangled_delimiter_falls_back_to_per_field_translation(monkeypatch):
    """
    Pairing a subject with the wrong body would be worse than three extra
    calls, so a delimiter the provider ate triggers a per-field retry.
    """
    calls = {"n": 0}

    def fake(text, language):
        calls["n"] += 1
        # First (batched) call loses the delimiter entirely.
        if calls["n"] == 1:
            return translation.Translation(
                text="all one blob", language=language, provider="deepl", translated=True
            )
        return translation.Translation(
            text=f"fr:{text}", language=language, provider="deepl", translated=True
        )

    monkeypatch.setattr(translation, "translate", fake)
    out, translated, _ = translation.translate_fields(
        {"subject": "s", "body": "b"}, "fr"
    )
    assert translated
    assert out["subject"] == "fr:s"
    assert out["body"] == "fr:b"


# =========================================================================== #
# Email sending
# =========================================================================== #

def test_brevo_is_selected_when_configured(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "fake")
    sender = email_sender.get_sender(EXAMPLE, provider="brevo")
    assert isinstance(sender, email_sender.BrevoSender)


def test_brevo_non_2xx_raises(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "fake")

    class Response:
        status_code = 400
        text = "bad request"

    monkeypatch.setattr(email_sender.requests, "post", lambda *a, **k: Response())
    sender = email_sender.BrevoSender(EXAMPLE, 10)
    sender.counter.reset()
    with pytest.raises(RuntimeError, match="400"):
        sender.send(to="a@b.com", subject="s", body="b", from_name="X", from_email="x@y.com")


def test_brevo_success_consumes_budget(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", "fake")

    class Response:
        status_code = 201

        @staticmethod
        def json():
            return {"messageId": "abc123"}

    monkeypatch.setattr(email_sender.requests, "post", lambda *a, **k: Response())
    sender = email_sender.BrevoSender(EXAMPLE, 10)
    sender.counter.reset()
    result = sender.send(
        to="a@b.com", subject="s", body="b", from_name="X", from_email="x@y.com"
    )
    assert result.ok and result.message_id == "abc123"
    assert sender.counter.used == 1


def test_gmail_without_credentials_raises_a_helpful_error(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    sender = email_sender.GmailSMTPSender(EXAMPLE, 10)
    sender.counter.reset()
    with pytest.raises(RuntimeError, match="App Password"):
        sender.send(to="a@b.com", subject="s", body="b", from_name="X", from_email="x@y.com")


# =========================================================================== #
# Email reading
# =========================================================================== #

def test_no_mailbox_configured_yields_a_null_reader(monkeypatch):
    monkeypatch.delenv("IMAP_USERNAME", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    reader = email_reader.get_reader()
    assert isinstance(reader, email_reader.NullReader)
    assert reader.fetch_replies("a@b.com", datetime.now(timezone.utc)) == []


def test_a_dry_run_never_polls_a_mailbox(monkeypatch):
    monkeypatch.setenv("IMAP_USERNAME", "x@gmail.com")
    monkeypatch.setenv("IMAP_PASSWORD", "pw")
    assert isinstance(email_reader.get_reader(dry_run=True), email_reader.NullReader)


def test_imap_is_selected_from_gmail_credentials(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "x@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    assert isinstance(email_reader.get_reader(), email_reader.IMAPReader)


def test_multipart_body_extraction_prefers_plain_text():
    import email as email_module
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = "Re: hello"
    message.set_content("the plain text reply")
    message.add_alternative("<html><body>the html reply</body></html>", subtype="html")
    parsed = email_module.message_from_bytes(message.as_bytes())
    assert "plain text" in email_reader._body_from_message(parsed)


def test_address_extraction_from_a_display_name_header():
    assert email_reader._extract_address("Dana Whitfield <Dana@Lakeside.com>") == (
        "dana@lakeside.com"
    )


def test_a_naive_date_header_is_made_aware():
    parsed = email_reader._parse_date("Mon, 2 Mar 2026 10:00:00")
    assert parsed.tzinfo is not None


# =========================================================================== #
# CRM
# =========================================================================== #

def test_crm_without_credentials_degrades_to_csv(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    backend = sheets_crm.get_backend(EXAMPLE)
    assert isinstance(backend, sheets_crm.DryRunCRM)


def test_google_sheets_is_selected_per_tenant(monkeypatch):
    monkeypatch.setenv(f"GOOGLE_SHEETS_SPREADSHEET_ID__{EXAMPLE}", "sheet-id")
    backend = sheets_crm.get_backend(EXAMPLE)
    assert isinstance(backend, sheets_crm.GoogleSheetsCRM)
    assert backend.spreadsheet_id == "sheet-id"


def test_one_tenants_sheet_id_is_not_used_for_another(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEETS_SPREADSHEET_ID__tenant_a", "sheet-a")
    monkeypatch.delenv("GOOGLE_SHEETS_SPREADSHEET_ID", raising=False)
    assert isinstance(sheets_crm.get_backend("tenant_b"), sheets_crm.DryRunCRM)


def test_airtable_is_selected_when_configured(monkeypatch):
    monkeypatch.setenv("AIRTABLE_API_KEY", "fake")
    monkeypatch.setenv(f"AIRTABLE_BASE_ID__{EXAMPLE}", "base-id")
    backend = sheets_crm.get_backend(EXAMPLE, backend="airtable")
    assert isinstance(backend, sheets_crm.AirtableCRM)


def test_upsert_many_survives_one_bad_lead(monkeypatch, tmp_path):
    backend = sheets_crm.DryRunCRM(EXAMPLE, tmp_path / "crm.csv")
    good = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="Good"
    )
    calls = {"n": 0}
    original = backend.upsert_lead

    def sometimes_fails(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sheets error")
        return original(state)

    monkeypatch.setattr(backend, "upsert_lead", sometimes_fails)
    written = backend.upsert_many([good, good])
    assert written == 1, "the second lead must still be written"


def test_the_csv_header_is_the_looker_contract(tmp_path):
    backend = sheets_crm.DryRunCRM(EXAMPLE, tmp_path / "crm.csv")
    backend.upsert_lead(new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X"
    ))
    import csv

    with (tmp_path / "crm.csv").open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header == list(CRM_COLUMNS)
