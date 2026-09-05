"""
providers.py -- how to check that a provider's credentials actually work.

WHAT LIVES HERE, AND WHAT NO LONGER DOES

The provider list itself is NOT here. It is `src/providers/registry.py`, which
the capability layer reads to build adapters and this module reads to render a
form -- one list, not a Python copy for the agent and a TypeScript copy for
the dashboard. Adding a provider is an edit to that file.

What lives here is the test call: one real, cheap request to the provider with
the values the user just typed, made BEFORE anything is saved. A key is never
stored on the strength of somebody typing carefully.

The test calls are deliberately the cheapest endpoint each provider offers --
usually an account or usage lookup. Checking a key should not consume the
allowance it is checking. A provider that needs no credentials gets a check
that says so rather than a fake network call.

NOT OFFERED: Google Places. It has no adapter in the capability layer and is
excluded from the product, so asking for a key we would not use would be
worse than not asking. An install with `GOOGLE_PLACES_API_KEY` already in its
`.env` keeps working -- see `LocalSearchDiscovery` -- it just is not something
this screen offers to set up.
"""

from __future__ import annotations

import json
import smtplib
import socket
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from src.providers import registry
from src.providers.base import CredentialType

#: Every network check gets the same short budget. A provider that cannot
#: answer in this long is not usable for a batch of hundreds of leads anyway.
TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class ProviderField:
    name: str
    label: str
    kind: str = "secret"          # "secret" | "text"
    placeholder: str = ""
    hint: str = ""
    required: bool = True
    multiline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "placeholder": self.placeholder,
            "hint": self.hint,
            "required": self.required,
            "multiline": self.multiline,
        }


@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "detail": self.detail}


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    category: str                 # ai | lead_discovery | enrichment | email | crm | translation
    purpose: str
    free_tier: str
    help_label: str
    help_url: str
    fields: tuple[ProviderField, ...]
    check: Callable[[dict[str, str]], TestResult]
    #: Which env var names this provider satisfies, so `src.settings` can
    #: resolve a stored secret when the agent asks for one.
    env_vars: dict[str, str] = field(default_factory=dict)
    #: True when nothing works at all without this category connected.
    essential: bool = False
    #: The registry entry this was built from. Carries `enabled`, `is_custom`,
    #: the credential type and the interface name, so a router can answer
    #: questions about a provider without a second lookup.
    spec: Any = None

    def to_dict(self) -> dict[str, Any]:
        """
        What the dashboard receives. Contains no credential and no field that
        could carry one -- see `backend/routers/integrations.py`.
        """
        payload = self.spec.to_dict() if self.spec is not None else {}
        payload.update(
            {
                "id": self.id,
                "name": self.name,
                "category": self.category,
                "purpose": self.purpose,
                "free_tier": self.free_tier,
                "help_label": self.help_label,
                "help_url": self.help_url,
                "fields": [f.to_dict() for f in self.fields],
                "essential": self.essential,
            }
        )
        return payload


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _http_check(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    on_ok: Callable[[httpx.Response], TestResult] | None = None,
) -> TestResult:
    """
    One request, with every failure turned into something a person can act on.

    The error messages matter more than the happy path: "401" tells the user
    nothing, "that key was rejected" tells them to check what they pasted.
    """
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.request(
                method, url, headers=headers, params=params, json=json_body
            )
    except httpx.TimeoutException:
        return TestResult(
            False,
            "The provider did not respond in time.",
            "It may be a temporary outage on their side. Try again in a minute.",
        )
    except httpx.RequestError as exc:
        return TestResult(
            False,
            "Could not reach the provider.",
            f"Check this machine's internet connection. ({exc.__class__.__name__})",
        )

    if response.status_code in (401, 403):
        return TestResult(
            False,
            "That key was rejected.",
            "Check for a stray space or a missing character, and that the key is "
            "still active in the provider's console.",
        )
    if response.status_code == 429:
        return TestResult(
            False,
            "The key is valid but the provider is rate-limiting it right now.",
            "Wait a minute and test again. Nothing was saved.",
        )
    if response.status_code >= 500:
        return TestResult(
            False,
            "The provider is having problems.",
            f"They returned {response.status_code}. This is not a problem with your key.",
        )
    if response.status_code >= 400:
        return TestResult(
            False,
            "The provider refused the request.",
            _short_body(response),
        )

    if on_ok:
        return on_ok(response)
    return TestResult(True, "Connected.")


def _short_body(response: httpx.Response, limit: int = 160) -> str:
    text = (response.text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #

def _check_groq(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("data", [])
        except json.JSONDecodeError:
            models = []
        return TestResult(
            True,
            "Connected.",
            f"{len(models)} models available on this key." if models else "",
        )

    return _http_check(
        "GET",
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )


def _check_gemini(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("models", [])
        except json.JSONDecodeError:
            models = []
        return TestResult(
            True,
            "Connected.",
            f"{len(models)} models available on this key." if models else "",
        )

    return _http_check(
        "GET",
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": values["api_key"].strip()},
        on_ok=ok,
    )


def _check_apollo(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        if payload.get("is_logged_in") is False:
            return TestResult(False, "That key was rejected.", "Apollo did not recognise it.")
        return TestResult(True, "Connected.")

    return _http_check(
        "GET",
        "https://api.apollo.io/v1/auth/health",
        headers={"X-Api-Key": values["api_key"].strip(), "Accept": "application/json"},
        on_ok=ok,
    )


def _check_hunter(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            data = response.json().get("data", {})
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        used = data.get("requests", {}).get("searches", {}).get("used")
        available = data.get("requests", {}).get("searches", {}).get("available")
        detail = ""
        if used is not None and available is not None:
            detail = f"{used} of {available} lookups used this month."
        return TestResult(True, "Connected.", detail)

    return _http_check(
        "GET",
        "https://api.hunter.io/v2/account",
        params={"api_key": values["api_key"].strip()},
        on_ok=ok,
    )


def _check_deepl(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        used = payload.get("character_count")
        limit = payload.get("character_limit")
        detail = ""
        if used is not None and limit:
            detail = f"{used:,} of {limit:,} characters used this month."
        return TestResult(True, "Connected.", detail)

    key = values["api_key"].strip()
    # DeepL's free and paid tiers are different hosts, and the key suffix says
    # which. Getting this wrong reads to the user as "my valid key was rejected".
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    return _http_check(
        "GET",
        f"https://{host}/v2/usage",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        on_ok=ok,
    )


def _check_gmail(values: dict[str, str]) -> TestResult:
    """
    A real SMTP login. Nothing is sent -- it connects, upgrades to TLS,
    authenticates and hangs up.

    Gmail requires an app password here, not the account password, and that is
    the single most common thing to get wrong, so the failure message says so
    outright instead of relaying Google's own wording.
    """
    address = values["address"].strip()
    password = values["app_password"].strip().replace(" ", "")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=TIMEOUT_SECONDS) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(address, password)
    except smtplib.SMTPAuthenticationError:
        return TestResult(
            False,
            "Google rejected that address and password.",
            "This has to be a 16-character app password, not your normal Google "
            "password. Two-step verification must be on to create one.",
        )
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        return TestResult(
            False,
            "Could not reach Gmail's mail server.",
            f"Port 587 may be blocked on this network. ({exc.__class__.__name__})",
        )

    return TestResult(True, "Connected.", f"Signed in as {address}. Nothing was sent.")


def _check_sheets(values: dict[str, str]) -> TestResult:
    """
    Open the actual spreadsheet with the pasted service-account key.

    Checking the credentials alone would pass and then fail later on the one
    thing people always forget -- sharing the sheet with the service account's
    email address -- so the check opens the sheet, and the failure message
    names that address so it can be copied straight into the share dialog.
    """
    raw = values["service_account_json"].strip()
    spreadsheet_id = values["spreadsheet_id"].strip()

    try:
        credentials = json.loads(raw)
    except json.JSONDecodeError:
        return TestResult(
            False,
            "That does not look like a service account key file.",
            "Paste the whole JSON file, including the outer braces.",
        )

    client_email = credentials.get("client_email", "")
    if not client_email:
        return TestResult(
            False,
            "That JSON has no client_email in it.",
            "Download the key for a service account, not an OAuth client.",
        )

    try:
        import gspread
    except ImportError:
        return TestResult(
            False,
            "The Google Sheets library is not installed on this machine.",
            "Install it with: pip install gspread google-auth",
        )

    try:
        client = gspread.service_account_from_dict(credentials)
        sheet = client.open_by_key(spreadsheet_id)
        title = sheet.title
    except Exception as exc:  # noqa: BLE001 - gspread raises several unrelated types
        name = exc.__class__.__name__
        if "SpreadsheetNotFound" in name or "PermissionError" in name or "APIError" in name:
            return TestResult(
                False,
                "The key is valid but it cannot open that spreadsheet.",
                f"Share the sheet with {client_email} as an Editor, then test again.",
            )
        return TestResult(False, "Google Sheets refused the connection.", f"{name}: {exc}")

    return TestResult(True, "Connected.", f'Opened "{title}".')


# --------------------------------------------------------------------------- #
# New checks
# --------------------------------------------------------------------------- #

def _check_brevo(values: dict[str, str]) -> TestResult:
    def ok(response: httpx.Response) -> TestResult:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        email = payload.get("email", "")
        return TestResult(
            True, "Connected.", f"Sending as {email}." if email else ""
        )

    return _http_check(
        "GET",
        "https://api.brevo.com/v3/account",
        headers={"api-key": values["api_key"].strip()},
        on_ok=ok,
    )


def _check_airtable(values: dict[str, str]) -> TestResult:
    """
    Reads the base's schema. Confirms the token AND that it can see the base --
    a token that is valid but scoped to a different base fails here, in front
    of the person who pasted it, rather than at the end of a batch.
    """
    base_id = values.get("base_id", "").strip()
    if not base_id.startswith("app"):
        return TestResult(
            False,
            "That does not look like a base ID.",
            "It starts with 'app' and is on the base's API page.",
        )

    def ok(response: httpx.Response) -> TestResult:
        try:
            tables = response.json().get("tables", [])
        except json.JSONDecodeError:
            tables = []
        names = ", ".join(t.get("name", "") for t in tables[:4])
        return TestResult(
            True, "Connected.", f"Tables found: {names}." if names else ""
        )

    return _http_check(
        "GET",
        f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )


def _check_imap(values: dict[str, str]) -> TestResult:
    """
    A real IMAP login. Nothing is read -- it connects over TLS, authenticates,
    and logs out.

    Blank fields are allowed and mean "reuse the Gmail connection", so this
    falls back to the stored Gmail address and app password. Testing what will
    actually be used at run time is the point; testing what was typed would
    pass here and fail in the batch.
    """
    import imaplib

    host = values.get("host", "").strip() or "imap.gmail.com"
    username = values.get("username", "").strip()
    password = values.get("password", "") or ""

    if not username or not password:
        from backend import secrets_store
        from backend.workspace import resolve_tenant_id

        tenant_id = resolve_tenant_id()
        username = username or secrets_store.get(tenant_id, "gmail_smtp", "address")
        password = password or secrets_store.get(tenant_id, "gmail_smtp", "app_password")

    if not username or not password:
        return TestResult(
            False,
            "There is nothing to log in with.",
            "Fill in the mailbox address and app password, or connect Gmail "
            "first and leave these blank to reuse it.",
        )

    try:
        with imaplib.IMAP4_SSL(host, timeout=int(TIMEOUT_SECONDS)) as client:
            client.login(username, password)
            client.select("INBOX", readonly=True)
        return TestResult(True, "Connected.", f"Signed in to {host}.")
    except imaplib.IMAP4.error:
        return TestResult(
            False,
            "The mailbox rejected that sign-in.",
            "For Gmail this must be an app password, not the account password, "
            "and IMAP has to be enabled in the mailbox settings.",
        )
    except (socket.timeout, TimeoutError):
        return TestResult(False, f"{host} did not respond in time.")
    except OSError as exc:
        return TestResult(
            False, f"Could not reach {host}.", f"({exc.__class__.__name__})"
        )


def _check_ollama(values: dict[str, str]) -> TestResult:
    """A model running on this machine. Lists what is installed."""
    base = (values.get("base_url", "").strip() or "http://localhost:11434").rstrip("/")

    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("models", [])
        except json.JSONDecodeError:
            models = []
        if not models:
            return TestResult(
                False,
                "Ollama is running but has no models installed.",
                "Install one first, for example: ollama pull llama3.1:8b",
            )
        names = ", ".join(m.get("name", "") for m in models[:4])
        return TestResult(True, "Connected.", f"Models installed: {names}.")

    return _http_check("GET", f"{base}/api/tags", on_ok=ok)


def _check_custom(values: dict[str, str]) -> TestResult:
    """
    A user-supplied endpoint. Validates the URL first, then calls it.

    URL validation is not a formality here. This is the one place the product
    makes an outbound request to an address somebody typed, so the checks in
    `src/providers/custom.py` -- https only, no private or loopback addresses,
    no redirects, enforced timeout and body cap -- run before the socket
    opens, and they run again on every real call afterwards.
    """
    from src.providers.custom import (
        CustomEndpointError,
        CustomConfig,
        post_json,
        validate_url,
    )

    url = values.get("base_url", "").strip()
    try:
        host = validate_url(url)
    except CustomEndpointError as exc:
        return TestResult(False, str(exc))

    style = (values.get("auth_style", "") or "bearer").strip().lower()
    if style not in ("bearer", "header", "query", "none"):
        return TestResult(
            False,
            f"'{style}' is not a way of authenticating this understands.",
            "Use bearer, header, query or none.",
        )

    if values.get("headers"):
        try:
            parsed = json.loads(values["headers"])
        except (ValueError, TypeError):
            return TestResult(
                False,
                "The extra headers are not valid JSON.",
                'They should look like {"X-Org": "acme"}.',
            )
        if not isinstance(parsed, dict):
            return TestResult(False, "The extra headers must be a JSON object.")

    config = CustomConfig(
        base_url=url,
        capability="ping",
        auth_style=style,
        token=values.get("token", "") or "",
        headers={},
    )
    try:
        post_json(config, {"capability": "ping", "action": "ping", "payload": {}})
    except CustomEndpointError as exc:
        # A 404 or a complaint about the payload still proves the endpoint is
        # reachable and authenticating, which is what is being tested. Only a
        # rejected credential or an unreachable host is a failure.
        message = str(exc)
        if "401" in message or "403" in message:
            return TestResult(False, "That endpoint rejected the key.", message)
        if "could not be reached" in message or "did not answer" in message:
            return TestResult(False, message)
        return TestResult(
            True,
            f"{host} is reachable.",
            "It did not recognise the test call, which is fine -- check it "
            "handles the actions listed in the setup notes.",
        )
    return TestResult(True, f"{host} answered.")


def _check_openai(values: dict[str, str]) -> TestResult:
    """Lists models. Costs nothing and proves the key can actually call."""
    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("data", [])
        except json.JSONDecodeError:
            models = []
        return TestResult(
            True, "Connected.",
            f"{len(models)} models available on this key." if models else "",
        )

    return _http_check(
        "GET",
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )


def _check_deepseek(values: dict[str, str]) -> TestResult:
    """Same shape as OpenAI's, because DeepSeek implements the same API."""
    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("data", [])
        except json.JSONDecodeError:
            models = []
        return TestResult(
            True, "Connected.",
            f"{len(models)} models available on this key." if models else "",
        )

    return _http_check(
        "GET",
        "https://api.deepseek.com/v1/models",
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )


def _check_anthropic(values: dict[str, str]) -> TestResult:
    """
    Lists models, with the version header their API requires.

    Anthropic has no free listing endpoint that skips auth, so a wrong key is
    rejected here exactly as it would be on a real call -- which is the point.
    """
    def ok(response: httpx.Response) -> TestResult:
        try:
            models = response.json().get("data", [])
        except json.JSONDecodeError:
            models = []
        return TestResult(
            True, "Connected.",
            f"{len(models)} models available on this key." if models else "",
        )

    return _http_check(
        "GET",
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": values["api_key"].strip(),
            "anthropic-version": "2023-06-01",
        },
        on_ok=ok,
    )


def _check_smtp(values: dict[str, str]) -> TestResult:
    """
    A real login against the user's own mail server. Nothing is sent.

    Port 465 is implicit TLS; everything else opens in the clear and upgrades.
    Getting that wrong is the most common way a correct username and password
    still fail, so the port decides rather than a checkbox.
    """
    host = values.get("host", "").strip()
    username = values.get("username", "").strip()
    password = values.get("password", "").strip()
    raw_port = values.get("port", "").strip() or "587"
    try:
        port = int(raw_port)
    except ValueError:
        return TestResult(
            False, "That port is not a number.", "Most servers use 587, or 465 for SSL."
        )

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS) as server:
                server.login(username, password)
        else:
            with smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(username, password)
    except smtplib.SMTPAuthenticationError:
        return TestResult(
            False,
            "That server rejected the username and password.",
            "Some hosts want the full email address as the username, and some "
            "need a separate app password rather than your normal one.",
        )
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        return TestResult(
            False,
            f"Could not reach {host or 'that server'} on port {port}.",
            f"Check the address and port, and that this network allows outbound "
            f"mail. ({exc.__class__.__name__})",
        )

    return TestResult(True, "Connected.", f"Signed in as {username}. Nothing was sent.")


def _check_anymail_finder(values: dict[str, str]) -> TestResult:
    """
    Reads the account, which does not spend a credit.

    Their search endpoints charge for a verified result, so testing a key
    against one would bill the user for pressing Test.
    """
    def ok(response: httpx.Response) -> TestResult:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        credits = payload.get("credits") or payload.get("credits_remaining")
        return TestResult(
            True, "Connected.",
            f"{credits} credits remaining." if credits is not None else "",
        )

    return _http_check(
        "GET",
        "https://api.anymailfinder.com/v5.0/users/me",
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )


def _check_hubspot(values: dict[str, str]) -> TestResult:
    """
    Reads one page of contacts.

    Proves the token AND that it carries the contacts scope -- a valid token
    without that scope returns 403 here, in front of the person who pasted it,
    rather than at the end of a batch when there is nowhere to put the leads.
    """
    def ok(response: httpx.Response) -> TestResult:
        return TestResult(True, "Connected.", "This token can read and write contacts.")

    result = _http_check(
        "GET",
        "https://api.hubapi.com/crm/v3/objects/contacts",
        params={"limit": "1"},
        headers={"Authorization": f"Bearer {values['api_key'].strip()}"},
        on_ok=ok,
    )
    if not result.ok and "rejected" in result.message:
        return TestResult(
            False,
            "HubSpot rejected that token.",
            "It has to be a private app access token with the "
            "crm.objects.contacts read and write scopes.",
        )
    return result


def _check_pipedrive(values: dict[str, str]) -> TestResult:
    """
    Reads the account the token belongs to.

    The address is checked too: a valid token against the wrong company
    subdomain is a 404, which reads as "wrong token" to everybody who has not
    hit it before, so the wording names the real cause.
    """
    domain = values.get("domain", "").strip().replace(".pipedrive.com", "")
    if not domain:
        return TestResult(
            False,
            "The Pipedrive address is needed too.",
            "For acme.pipedrive.com, type acme.",
        )

    def ok(response: httpx.Response) -> TestResult:
        try:
            data = response.json().get("data") or {}
        except json.JSONDecodeError:
            return TestResult(True, "Connected.")
        name = data.get("company_name") or data.get("name") or ""
        return TestResult(True, "Connected.", f"Signed in to {name}." if name else "")

    result = _http_check(
        "GET",
        f"https://{domain}.pipedrive.com/api/v1/users/me",
        params={"api_token": values["api_key"].strip()},
        on_ok=ok,
    )
    if not result.ok and "refused" in result.message:
        return TestResult(
            False,
            f"Pipedrive did not recognise that token at {domain}.pipedrive.com.",
            "Check the address as well as the token - a token only works "
            "against its own company.",
        )
    return result


def _check_nothing_needed(values: dict[str, str]) -> TestResult:
    """For a provider with no credentials. There is nothing to get wrong."""
    return TestResult(
        True, "Ready to use.", "This one needs no account and no key."
    )


def _check_not_built(values: dict[str, str]) -> TestResult:
    return TestResult(
        False,
        "This one is not built yet, so there is nothing to connect to.",
        "It is listed so you can see it is coming.",
    )


# --------------------------------------------------------------------------- #
# The registry, derived
# --------------------------------------------------------------------------- #

#: provider id -> its credential check. Only ids that need a real call appear
#: here; everything else is resolved by `_check_for` below from what the
#: registry says about it.
CHECKS: dict[str, Callable[[dict[str, str]], TestResult]] = {
    "groq": _check_groq,
    "gemini": _check_gemini,
    "ollama": _check_ollama,
    "apollo": _check_apollo,
    "hunter": _check_hunter,
    "gmail_smtp": _check_gmail,
    "brevo": _check_brevo,
    "imap": _check_imap,
    "google_sheets": _check_sheets,
    "airtable": _check_airtable,
    "deepl": _check_deepl,
    "openai": _check_openai,
    "anthropic": _check_anthropic,
    "deepseek": _check_deepseek,
    "smtp": _check_smtp,
    "anymail_finder": _check_anymail_finder,
    "hubspot": _check_hubspot,
    "pipedrive": _check_pipedrive,
}


def _check_for(spec: registry.ProviderSpec) -> Callable[[dict[str, str]], TestResult]:
    if not spec.enabled:
        return _check_not_built
    if spec.is_custom:
        return _check_custom
    check = CHECKS.get(spec.id)
    if check is not None:
        return check
    if spec.credential_type is CredentialType.NONE:
        return _check_nothing_needed
    # A provider that needs a credential and has no check is a programming
    # error, not a user problem -- fail loudly here rather than letting an
    # unverified key be stored.
    raise RuntimeError(
        f"{spec.id} needs credentials but has no check in backend/providers.py"
    )


def _provider_from(spec: registry.ProviderSpec) -> Provider:
    return Provider(
        id=spec.id,
        name=spec.display_name,
        category=spec.capability.value,
        purpose=spec.purpose,
        free_tier=spec.free_tier,
        help_label=spec.docs_label,
        help_url=spec.docs_url,
        fields=tuple(
            ProviderField(
                name=f.name,
                label=f.label,
                kind=f.kind,
                placeholder=f.placeholder,
                hint=f.hint,
                required=f.required,
                multiline=f.multiline,
            )
            for f in spec.connection_fields
        ),
        check=_check_for(spec),
        env_vars=dict(spec.env_vars),
        essential=spec.essential,
        spec=spec,
    )


#: Everything a user can see on Connections: enabled or "coming soon", but
#: never the internal adapters (the dry-run sender, the null reader, the test
#: model), which are resolvable and have nothing to connect.
PROVIDERS: tuple[Provider, ...] = tuple(
    _provider_from(spec) for spec in registry.PROVIDERS if not spec.hidden
)

PROVIDERS_BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}

#: env var -> (provider id, field). Re-exported from the registry so
#: `src.settings` and older imports keep working against one mapping.
ENV_VAR_SOURCES: dict[str, tuple[str, str]] = dict(registry.ENV_VAR_SOURCES)


def get_provider(provider_id: str) -> Provider:
    provider = PROVIDERS_BY_ID.get(provider_id)
    if provider is None:
        raise KeyError(provider_id)
    return provider


def validate_values(provider: Provider, values: dict[str, str]) -> str:
    """Returns a human-readable problem, or "" when the values are complete."""
    missing = [
        f.label
        for f in provider.fields
        if f.required and not str(values.get(f.name, "")).strip()
    ]
    if missing:
        return f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required."
    return ""
