"""
What each provider's connection test actually sends.

These exist because the checks in `backend/providers.py` were the least-tested
code in the product and the most exposed: they are the first thing a new user
touches, they are the only thing standing between a typo and a key stored
against a provider that will reject it later, and a wrong URL or a
misremembered header name would have passed every other test in the suite.

Three things are asserted, per provider:

  the endpoint    the exact host and path, because "cheapest endpoint that
                  proves the key" is a decision per vendor and a plausible
                  wrong guess still returns 200 for some of them
  the auth        WHERE the credential goes -- Anthropic wants `x-api-key` and
                  rejects a bearer token, Hunter and Pipedrive want a query
                  parameter, Brevo wants `api-key`, Apollo wants `X-Api-Key`
  the reading     that a success is reported as success and the vendor's own
                  numbers reach the user

Nothing here touches the network: `respx` intercepts httpx for the fourteen
REST checks, and the four that speak SMTP, IMAP or gspread get their client
replaced. `assert_all_called` is on, and an unmatched request raises, so a
check that called some *other* URL as well would fail rather than pass
quietly.
"""

from __future__ import annotations

import smtplib
import sys

import httpx
import pytest
import respx

from backend import providers as bp
# Aliased: pytest tries to collect anything named Test* out of a test module,
# and a dataclass is not a test class.
from backend.providers import TestResult as CheckResult
from src.providers import registry


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

def run_check(provider_id: str, values: dict[str, str]) -> CheckResult:
    """
    Resolve the check the same way the router does, then run it.

    Going through `_check_for` rather than calling `_check_*` directly means
    these tests also cover the dispatch: a provider silently wired to the
    wrong check would fail here.
    """
    spec = registry.PROVIDERS_BY_ID[provider_id]
    return bp._check_for(spec)(values)


def rest_call(
    provider_id: str,
    values: dict[str, str],
    *,
    host: str,
    path: str,
    status: int = 200,
    json_body: object | None = None,
) -> tuple[CheckResult, httpx.Request]:
    """Run a REST check against one mocked route and hand back both sides."""
    with respx.mock(assert_all_called=True) as mock:
        route = mock.route(method="GET", host=host, path=path).mock(
            return_value=httpx.Response(status, json=json_body if json_body is not None else {})
        )
        result = run_check(provider_id, values)
    return result, route.calls.last.request


# --------------------------------------------------------------------------- #
# The fourteen REST checks
# --------------------------------------------------------------------------- #

def test_groq_lists_models_with_a_bearer_token():
    result, request = rest_call(
        "groq",
        {"api_key": "  gsk_live  "},
        host="api.groq.com",
        path="/openai/v1/models",
        json_body={"data": [{"id": "a"}, {"id": "b"}]},
    )
    assert result.ok
    # Whitespace from a paste is stripped before it reaches the header.
    assert request.headers["authorization"] == "Bearer gsk_live"
    assert "2 models" in result.detail


def test_gemini_carries_the_key_as_a_query_parameter():
    """
    Google AI Studio authenticates with `?key=`, not a header.

    A bearer token against this endpoint returns 401, so getting this wrong
    reads to the user as "my valid key was rejected".
    """
    result, request = rest_call(
        "gemini",
        {"api_key": "AIzaKEY"},
        host="generativelanguage.googleapis.com",
        path="/v1beta/models",
        json_body={"models": [{"name": "gemini-2.0-flash"}]},
    )
    assert result.ok
    assert request.url.params["key"] == "AIzaKEY"
    assert "authorization" not in request.headers
    assert "1 models" in result.detail


def test_openai_lists_models_with_a_bearer_token():
    result, request = rest_call(
        "openai",
        {"api_key": "sk-live"},
        host="api.openai.com",
        path="/v1/models",
        json_body={"data": [{"id": "gpt-4o-mini"}]},
    )
    assert result.ok
    assert request.headers["authorization"] == "Bearer sk-live"


def test_deepseek_uses_its_own_host_not_openais():
    """
    DeepSeek implements OpenAI's API, which makes the host the only thing
    distinguishing them -- and the easiest thing to copy-paste wrong.
    """
    result, request = rest_call(
        "deepseek",
        {"api_key": "sk-ds"},
        host="api.deepseek.com",
        path="/v1/models",
        json_body={"data": [{"id": "deepseek-chat"}]},
    )
    assert result.ok
    assert request.url.host == "api.deepseek.com"
    assert request.headers["authorization"] == "Bearer sk-ds"


def test_anthropic_uses_x_api_key_and_the_version_header():
    """
    Anthropic rejects a bearer token and rejects a request with no
    `anthropic-version`. Both are easy to get wrong from muscle memory.
    """
    result, request = rest_call(
        "anthropic",
        {"api_key": "sk-ant-live"},
        host="api.anthropic.com",
        path="/v1/models",
        json_body={"data": [{"id": "claude-sonnet-4-5"}]},
    )
    assert result.ok
    assert request.headers["x-api-key"] == "sk-ant-live"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers


def test_apollo_uses_its_health_endpoint_and_reads_the_flag():
    result, request = rest_call(
        "apollo",
        {"api_key": "apollo-key"},
        host="api.apollo.io",
        path="/v1/auth/health",
        json_body={"is_logged_in": True},
    )
    assert result.ok
    assert request.headers["x-api-key"] == "apollo-key"


def test_apollo_reports_a_200_that_says_not_logged_in_as_a_failure():
    """
    Apollo answers 200 with `is_logged_in: false` for a bad key.

    Trusting the status code alone would store a key that cannot search.
    """
    result, _ = rest_call(
        "apollo",
        {"api_key": "wrong"},
        host="api.apollo.io",
        path="/v1/auth/health",
        json_body={"is_logged_in": False},
    )
    assert not result.ok
    assert "rejected" in result.message


def test_hunter_reads_the_account_and_reports_the_quota():
    """
    The account endpoint, not a search: checking a key should not spend the
    25 lookups a month it is checking.
    """
    result, request = rest_call(
        "hunter",
        {"api_key": "hunter-key"},
        host="api.hunter.io",
        path="/v2/account",
        json_body={"data": {"requests": {"searches": {"used": 3, "available": 25}}}},
    )
    assert result.ok
    assert request.url.params["api_key"] == "hunter-key"
    assert result.detail == "3 of 25 lookups used this month."


def test_anymail_finder_checks_the_account_rather_than_a_search():
    """Their search endpoints bill per verified result. Test must be free."""
    result, request = rest_call(
        "anymail_finder",
        {"api_key": "amf-key"},
        host="api.anymailfinder.com",
        path="/v5.0/users/me",
        json_body={"credits": 100},
    )
    assert result.ok
    assert request.headers["authorization"] == "Bearer amf-key"
    assert "100 credits" in result.detail


def test_brevo_uses_the_api_key_header_and_reports_the_sender():
    result, request = rest_call(
        "brevo",
        {"api_key": "xkeysib-live"},
        host="api.brevo.com",
        path="/v3/account",
        json_body={"email": "owner@example.com"},
    )
    assert result.ok
    assert request.headers["api-key"] == "xkeysib-live"
    assert "owner@example.com" in result.detail


def test_airtable_reads_the_schema_of_the_base_it_was_given():
    result, request = rest_call(
        "airtable",
        {"api_key": "pat-live", "base_id": "appABC123"},
        host="api.airtable.com",
        path="/v0/meta/bases/appABC123/tables",
        json_body={"tables": [{"name": "Leads"}, {"name": "Runs"}]},
    )
    assert result.ok
    assert request.headers["authorization"] == "Bearer pat-live"
    assert "Leads, Runs" in result.detail


def test_airtable_rejects_a_base_id_before_making_any_call():
    """
    respx is active with no routes, so any request at all would raise. The
    point is that a malformed base id is caught locally rather than sent.
    """
    with respx.mock:
        result = run_check("airtable", {"api_key": "pat-live", "base_id": "tblWrong"})
    assert not result.ok
    assert "base ID" in result.message


def test_hubspot_reads_one_contact_to_prove_the_scope():
    result, request = rest_call(
        "hubspot",
        {"api_key": "pat-na1-live"},
        host="api.hubapi.com",
        path="/crm/v3/objects/contacts",
        json_body={"results": []},
    )
    assert result.ok
    assert request.headers["authorization"] == "Bearer pat-na1-live"
    # limit=1 keeps it cheap on a portal with a million contacts.
    assert request.url.params["limit"] == "1"


def test_hubspot_turns_a_403_into_the_scope_it_is_actually_missing():
    """
    A valid token without the contacts scope is the common HubSpot mistake,
    and "that key was rejected" sends the user looking for the wrong problem.
    """
    result, _ = rest_call(
        "hubspot",
        {"api_key": "pat-na1-live"},
        host="api.hubapi.com",
        path="/crm/v3/objects/contacts",
        status=403,
    )
    assert not result.ok
    assert "private app access token" in result.detail
    assert "crm.objects.contacts" in result.detail


def test_pipedrive_calls_the_companys_own_subdomain():
    result, request = rest_call(
        "pipedrive",
        {"api_key": "pd-token", "domain": "acme"},
        host="acme.pipedrive.com",
        path="/api/v1/users/me",
        json_body={"data": {"company_name": "Acme Ltd"}},
    )
    assert result.ok
    assert request.url.params["api_token"] == "pd-token"
    assert "Acme Ltd" in result.detail


def test_pipedrive_accepts_a_full_host_as_the_domain():
    """People paste `acme.pipedrive.com`. Both forms have to work."""
    result, request = rest_call(
        "pipedrive",
        {"api_key": "pd-token", "domain": "acme.pipedrive.com"},
        host="acme.pipedrive.com",
        path="/api/v1/users/me",
        json_body={"data": {}},
    )
    assert result.ok
    assert request.url.host == "acme.pipedrive.com"


def test_pipedrive_asks_for_the_address_before_calling_anything():
    with respx.mock:
        result = run_check("pipedrive", {"api_key": "pd-token", "domain": "  "})
    assert not result.ok
    assert "address" in result.message


def test_pipedrive_blames_the_subdomain_on_a_404_rather_than_the_token():
    """
    A good token against the wrong company is a 404, which everybody reads as
    "bad token" the first time they hit it.
    """
    result, _ = rest_call(
        "pipedrive",
        {"api_key": "pd-token", "domain": "wrongco"},
        host="wrongco.pipedrive.com",
        path="/api/v1/users/me",
        status=404,
    )
    assert not result.ok
    assert "wrongco.pipedrive.com" in result.message
    assert "address as well as the token" in result.detail


def test_deepl_sends_a_free_key_to_the_free_host():
    """
    DeepL's tiers are different hosts and the `:fx` suffix is the only signal.
    Sent to the wrong one, a perfectly good key comes back rejected.
    """
    result, request = rest_call(
        "deepl",
        {"api_key": "abc-123:fx"},
        host="api-free.deepl.com",
        path="/v2/usage",
        json_body={"character_count": 1000, "character_limit": 500000},
    )
    assert result.ok
    assert request.headers["authorization"] == "DeepL-Auth-Key abc-123:fx"
    assert "1,000 of 500,000 characters" in result.detail


def test_deepl_sends_a_paid_key_to_the_paid_host():
    result, request = rest_call(
        "deepl",
        {"api_key": "abc-123"},
        host="api.deepl.com",
        path="/v2/usage",
        json_body={"character_count": 0, "character_limit": 1000000},
    )
    assert result.ok
    assert request.url.host == "api.deepl.com"


def test_ollama_lists_what_is_installed_on_this_machine():
    result, request = rest_call(
        "ollama",
        {"base_url": ""},
        host="localhost",
        path="/api/tags",
        json_body={"models": [{"name": "llama3.1:8b"}]},
    )
    assert result.ok
    # Blank means the default, which is where Ollama listens out of the box.
    assert request.url.port == 11434
    assert "llama3.1:8b" in result.detail


def test_ollama_running_with_no_models_is_not_a_working_connection():
    """
    Reachable is not the same as usable. Reporting success here would store a
    provider that fails on the first real call with nothing to run.
    """
    result, _ = rest_call(
        "ollama",
        {"base_url": "http://localhost:11434"},
        host="localhost",
        path="/api/tags",
        json_body={"models": []},
    )
    assert not result.ok
    assert "no models installed" in result.message


# --------------------------------------------------------------------------- #
# The error ladder, once, for the thirteen checks that share it
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "status, expect_ok, in_message",
    [
        (200, True, "Connected"),
        (400, False, "refused the request"),
        (401, False, "rejected"),
        (403, False, "rejected"),
        (404, False, "refused the request"),
        (429, False, "rate-limiting"),
        (500, False, "having problems"),
        (503, False, "having problems"),
    ],
)
def test_the_shared_error_ladder_maps_every_status_to_a_sentence(
    status: int, expect_ok: bool, in_message: str
):
    """
    Every REST check funnels its failures through `_http_check`, so this is
    the wording users see for all thirteen of them. A status code is not a
    remedy; each rung has to say what to do next.
    """
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="GET", host="example.test").mock(
            return_value=httpx.Response(status, json={})
        )
        result = bp._http_check("GET", "https://example.test/ping")

    assert result.ok is expect_ok
    assert in_message in result.message


def test_a_429_says_nothing_was_saved():
    """
    Rate-limited means "the key is fine, try again", and the user needs to
    know their key was not stored -- otherwise they retype it.
    """
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="GET", host="example.test").mock(
            return_value=httpx.Response(429, json={})
        )
        result = bp._http_check("GET", "https://example.test/ping")
    assert "Nothing was saved" in result.detail


def test_a_4xx_body_is_passed_through_but_truncated():
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="GET", host="example.test").mock(
            return_value=httpx.Response(422, text="x" * 400)
        )
        result = bp._http_check("GET", "https://example.test/ping")
    assert not result.ok
    assert len(result.detail) <= 161
    assert result.detail.endswith("…")


@pytest.mark.parametrize(
    "exc, in_message",
    [
        (httpx.ConnectTimeout("slow"), "did not respond in time"),
        (httpx.ReadTimeout("slow"), "did not respond in time"),
        (httpx.ConnectError("no route"), "Could not reach the provider"),
        (httpx.RemoteProtocolError("hung up"), "Could not reach the provider"),
    ],
)
def test_transport_failures_do_not_leak_an_exception_to_the_user(
    exc: Exception, in_message: str
):
    """
    A machine with no internet, or a provider that is down, must produce a
    remedy rather than a traceback -- this runs while somebody is watching a
    spinner in the browser.
    """
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="GET", host="example.test").mock(side_effect=exc)
        result = bp._http_check("GET", "https://example.test/ping")

    assert not result.ok
    assert in_message in result.message
    # The class name is context, never the whole message.
    assert "Traceback" not in result.detail


# --------------------------------------------------------------------------- #
# Gmail and any SMTP mailbox -- a real login, and nothing sent
# --------------------------------------------------------------------------- #

class FakeSMTP:
    """Records what a check did to it. Instances land in `FakeSMTP.log`."""

    log: list[dict] = []

    def __init__(self, host, port, timeout=None):
        self.entry = {
            "host": host,
            "port": port,
            "timeout": timeout,
            "starttls": False,
            "login": None,
            "sent": False,
            "ssl": type(self).__name__ == "FakeSMTPSSL",
        }
        FakeSMTP.log.append(self.entry)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self):
        self.entry["starttls"] = True

    def login(self, username, password):
        self.entry["login"] = (username, password)

    def sendmail(self, *args, **kwargs):  # pragma: no cover - must never run
        self.entry["sent"] = True
        raise AssertionError("a connection check must not send mail")


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp_log(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    FakeSMTP.log = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP.log


def test_gmail_logs_in_over_starttls_on_587_and_sends_nothing(smtp_log):
    result = run_check(
        "gmail_smtp",
        {"address": "me@gmail.com", "app_password": "abcd efgh ijkl mnop"},
    )
    assert result.ok
    entry = smtp_log[-1]
    assert (entry["host"], entry["port"]) == ("smtp.gmail.com", 587)
    assert entry["starttls"] is True
    # Google displays app passwords in groups of four; the spaces are not part
    # of the password and pasting them verbatim is the usual failure.
    assert entry["login"] == ("me@gmail.com", "abcdefghijklmnop")
    assert entry["sent"] is False
    assert "Nothing was sent" in result.detail


def test_gmail_names_the_app_password_when_google_refuses(monkeypatch):
    class Refusing(FakeSMTP):
        def login(self, username, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    FakeSMTP.log = []
    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    result = run_check("gmail_smtp", {"address": "me@gmail.com", "app_password": "x"})
    assert not result.ok
    # The single most common cause, said outright rather than relaying Google.
    assert "app password" in result.detail
    assert "Two-step verification" in result.detail


def test_smtp_on_465_uses_implicit_tls_and_does_not_call_starttls(smtp_log):
    """
    465 is TLS from the first byte. Calling STARTTLS on it fails, and that is
    the most common way a correct username and password still do not work --
    so the port decides rather than a checkbox.
    """
    result = run_check(
        "smtp",
        {"host": "mail.example.com", "port": "465", "username": "u", "password": "p"},
    )
    assert result.ok
    entry = smtp_log[-1]
    assert entry["ssl"] is True
    assert entry["starttls"] is False
    assert entry["login"] == ("u", "p")


def test_smtp_on_587_upgrades_in_the_clear(smtp_log):
    result = run_check(
        "smtp",
        {"host": "mail.example.com", "port": "587", "username": "u", "password": "p"},
    )
    assert result.ok
    entry = smtp_log[-1]
    assert entry["ssl"] is False
    assert entry["starttls"] is True


def test_smtp_defaults_to_587_when_the_port_is_left_blank(smtp_log):
    result = run_check(
        "smtp", {"host": "mail.example.com", "port": "", "username": "u", "password": "p"}
    )
    assert result.ok
    assert smtp_log[-1]["port"] == 587


def test_smtp_rejects_a_non_numeric_port_without_connecting(smtp_log):
    result = run_check(
        "smtp",
        {"host": "mail.example.com", "port": "five-eight-seven", "username": "u", "password": "p"},
    )
    assert not result.ok
    assert "not a number" in result.message
    assert smtp_log == []


# --------------------------------------------------------------------------- #
# IMAP -- a real login, and nothing read
# --------------------------------------------------------------------------- #

@pytest.fixture
def imap_log(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import imaplib

    log: list[dict] = []

    class FakeIMAP:
        def __init__(self, host, timeout=None):
            self.entry = {"host": host, "login": None, "select": None}
            log.append(self.entry)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, username, password):
            self.entry["login"] = (username, password)

        def select(self, mailbox, readonly=False):
            self.entry["select"] = (mailbox, readonly)

    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return log


def test_imap_logs_in_and_opens_the_inbox_read_only(imap_log):
    result = run_check(
        "imap", {"host": "imap.fastmail.com", "username": "me@x.com", "password": "pw"}
    )
    assert result.ok
    entry = imap_log[-1]
    assert entry["host"] == "imap.fastmail.com"
    assert entry["login"] == ("me@x.com", "pw")
    # readonly=True: a connection check must not mark anybody's mail as read.
    assert entry["select"] == ("INBOX", True)


def test_imap_falls_back_to_the_stored_gmail_connection(imap_log, monkeypatch):
    """
    Blank fields mean "reuse Gmail". Testing what was typed would pass and
    then fail in the batch, so the check resolves what will actually be used.
    """
    from backend import secrets_store

    monkeypatch.setattr(
        secrets_store,
        "get",
        lambda tenant, provider, field: {
            "address": "me@gmail.com",
            "app_password": "storedpw",
        }[field],
    )
    result = run_check("imap", {"host": "", "username": "", "password": ""})
    assert result.ok
    entry = imap_log[-1]
    assert entry["host"] == "imap.gmail.com"
    assert entry["login"] == ("me@gmail.com", "storedpw")


def test_imap_says_so_when_there_is_nothing_to_reuse(imap_log, monkeypatch):
    from backend import secrets_store

    monkeypatch.setattr(secrets_store, "get", lambda *a, **k: "")
    result = run_check("imap", {"host": "", "username": "", "password": ""})
    assert not result.ok
    assert "nothing to log in with" in result.message
    assert imap_log == []


# --------------------------------------------------------------------------- #
# Google Sheets -- open the actual sheet
# --------------------------------------------------------------------------- #

SERVICE_ACCOUNT = (
    '{"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com", '
    '"private_key": "-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----\\n"}'
)


@pytest.fixture
def fake_gspread(monkeypatch: pytest.MonkeyPatch):
    """
    `_check_sheets` imports gspread inside the function, so a stub module in
    `sys.modules` is enough and no real credentials are constructed.
    """
    import types

    calls: dict = {}

    class Sheet:
        title = "Leads 2026"

    class Client:
        def open_by_key(self, key):
            calls["opened"] = key
            if key == "missing":
                raise type("SpreadsheetNotFound", (Exception,), {})()
            return Sheet()

    module = types.ModuleType("gspread")
    module.service_account_from_dict = lambda creds: (
        calls.setdefault("creds", creds),
        Client(),
    )[1]
    monkeypatch.setitem(sys.modules, "gspread", module)
    return calls


def test_sheets_opens_the_spreadsheet_it_was_given(fake_gspread):
    """
    Validating the credentials alone would pass and then fail later on the one
    thing everybody forgets -- sharing the sheet with the service account.
    """
    result = run_check(
        "google_sheets",
        {"spreadsheet_id": "1AbC", "service_account_json": SERVICE_ACCOUNT},
    )
    assert result.ok
    assert fake_gspread["opened"] == "1AbC"
    assert "Leads 2026" in result.detail


def test_sheets_names_the_address_to_share_with_when_it_cannot_open(fake_gspread):
    """
    The remedy is an email address the user has to paste into Google's share
    dialog, so the failure hands it to them rather than describing it.
    """
    result = run_check(
        "google_sheets",
        {"spreadsheet_id": "missing", "service_account_json": SERVICE_ACCOUNT},
    )
    assert not result.ok
    assert "bot@proj.iam.gserviceaccount.com" in result.detail


def test_sheets_rejects_something_that_is_not_a_key_file():
    result = run_check(
        "google_sheets",
        {"spreadsheet_id": "1AbC", "service_account_json": "not json at all"},
    )
    assert not result.ok
    assert "service account key file" in result.message


def test_sheets_rejects_an_oauth_client_secret():
    """
    An OAuth client JSON parses fine and has no `client_email`. Without this
    the failure would come from deep inside gspread instead.
    """
    result = run_check(
        "google_sheets",
        {"spreadsheet_id": "1AbC", "service_account_json": '{"installed": {"client_id": "x"}}'},
    )
    assert not result.ok
    assert "client_email" in result.message


# --------------------------------------------------------------------------- #
# The custom endpoint -- the one URL the user supplies
# --------------------------------------------------------------------------- #

def test_a_custom_endpoint_is_validated_before_a_socket_opens():
    """
    This is the only place the product calls an address somebody typed, so the
    SSRF guards in `src/providers/custom.py` run first. respx is active with
    no routes: a request would raise rather than pass.
    """
    with respx.mock:
        result = run_check(
            "custom_llm",
            {"base_url": "http://127.0.0.1:8000/hook", "auth_style": "bearer", "token": "t"},
        )
    assert not result.ok


@pytest.fixture
def resolvable_url(monkeypatch: pytest.MonkeyPatch):
    """
    `validate_url` resolves the host, so a made-up domain fails there before
    the branch under test is reached. The SSRF guard itself is covered by
    `test_a_custom_endpoint_is_validated_before_a_socket_opens`, which needs
    no DNS; these cases are about what happens after it passes.
    """
    from src.providers import custom

    monkeypatch.setattr(custom, "validate_url", lambda url: "api.example.com")
    return None


def test_a_custom_endpoint_rejects_an_auth_style_it_cannot_perform(resolvable_url):
    with respx.mock:
        result = run_check(
            "custom_llm",
            {"base_url": "https://api.example.com/hook", "auth_style": "oauth2", "token": "t"},
        )
    assert not result.ok
    assert "bearer, header, query or none" in result.detail


def test_a_custom_endpoint_rejects_headers_that_are_not_json(resolvable_url):
    with respx.mock:
        result = run_check(
            "custom_llm",
            {
                "base_url": "https://api.example.com/hook",
                "auth_style": "bearer",
                "token": "t",
                "headers": "X-Org: acme",
            },
        )
    assert not result.ok
    assert "valid JSON" in result.message


def test_a_custom_endpoint_that_answers_is_a_pass(resolvable_url, monkeypatch):
    from src.providers import custom

    monkeypatch.setattr(custom, "post_json", lambda config, payload: {"ok": True})
    result = run_check(
        "custom_llm",
        {"base_url": "https://api.example.com/hook", "auth_style": "bearer", "token": "t"},
    )
    assert result.ok
    assert "api.example.com" in result.message


def test_a_custom_endpoint_that_rejects_the_key_is_a_failure(resolvable_url, monkeypatch):
    from src.providers import custom

    def refuse(config, payload):
        raise custom.CustomEndpointError("the endpoint returned 401")

    monkeypatch.setattr(custom, "post_json", refuse)
    result = run_check(
        "custom_llm",
        {"base_url": "https://api.example.com/hook", "auth_style": "bearer", "token": "t"},
    )
    assert not result.ok
    assert "rejected the key" in result.message


def test_a_custom_endpoint_that_does_not_know_the_ping_still_passes(resolvable_url, monkeypatch):
    """
    A 404 or a complaint about the payload still proves the host is reachable
    and authenticating, which is what Test is for. Only a rejected credential
    or an unreachable host is a failure.
    """
    from src.providers import custom

    def puzzled(config, payload):
        raise custom.CustomEndpointError("the endpoint returned 404")

    monkeypatch.setattr(custom, "post_json", puzzled)
    result = run_check(
        "custom_llm",
        {"base_url": "https://api.example.com/hook", "auth_style": "bearer", "token": "t"},
    )
    assert result.ok
    assert "reachable" in result.message


# --------------------------------------------------------------------------- #
# Providers with nothing to check
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "provider_id",
    ["osm", "csv_import", "website_only", "csv", "dry_run", "none", "mock", "llm"],
)
def test_a_credential_free_provider_reports_ready_without_calling_out(provider_id: str):
    """
    A fake network call here would be worse than none: it would make an
    always-available option look like it could fail.
    """
    with respx.mock:
        result = run_check(provider_id, {})
    assert result.ok
    assert "no account and no key" in result.detail


def test_every_provider_needing_a_credential_has_a_real_check():
    """
    The guard that makes the rest of this file exhaustive: a new provider with
    a credential and no check raises rather than silently storing an
    unverified key.
    """
    needs_credential = {
        spec.id
        for spec in registry.PROVIDERS
        if spec.credential_type is not registry.CredentialType.NONE and not spec.is_custom
    }
    missing = needs_credential - set(bp.CHECKS)
    assert not missing, f"these would store an unverified credential: {sorted(missing)}"

    # The reverse is not an error: `ollama` needs no credential but has a real
    # check, because "is a model actually installed on this machine" is worth
    # answering. What IS an error is a check for a provider that no longer
    # exists -- dead code that looks like coverage.
    orphaned = set(bp.CHECKS) - {spec.id for spec in registry.PROVIDERS}
    assert not orphaned, f"checks for providers that no longer exist: {sorted(orphaned)}"
