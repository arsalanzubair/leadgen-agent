"""
custom.py -- one generic REST adapter per capability.

This is the escape hatch: a buyer who already pays for a service this product
has never heard of points a capability at their own endpoint and it works,
with no code change and no release. It is also the only place in the whole
product that makes an outbound call to a URL a user typed, which is why the
security notes below are rules and not suggestions.

--------------------------------------------------------------------------
THE WIRE CONTRACT
--------------------------------------------------------------------------

Every capability except the model posts JSON to the configured URL in one
envelope, so an endpoint can serve several capabilities from one handler:

    POST <base_url>
    {"capability": "<capability>", "action": "<action>", "payload": {...}}

and answers `200` with a JSON object. Anything else -- a 4xx, a 5xx, HTML, a
truncated body -- is treated as "this provider could not answer", which for
every capability here means a logged skip, not an exception.

  capability          action              payload                     answer
  ------------------  ------------------  --------------------------  ---------------------------
  discovery_local     find                search_terms, locations,    {"results": [ ... ]}
  discovery_b2b       find                titles, industries,         {"results": [ ... ]}
                                          employee_range, limit
  enrichment          find_email          domain, first_name,         {"email", "confidence",
                                          last_name                    "first_name", "last_name",
                                                                       "position"}  or {} / null
  email_sender        send                to, to_name, subject,       {"ok": true,
                                          body, from_name,             "message_id": "..."}
                                          from_email, reply_to
  email_reader        fetch_replies       address, since (ISO 8601)   {"messages": [ ... ]}
  crm                 upsert_lead         lead (the full record)      {"ok": true, "ref": "..."}
  crm                 append_suppression  entry                       {"ok": true}
  translation         translate           text, language              {"text": "...",
                                                                       "translated": true}

The MODEL capability is the deliberate exception: `custom_llm` speaks the
OpenAI chat-completions shape instead.

    POST <base_url>
    {"model": "...", "messages": [{"role": "system"|"user", "content": "..."}],
     "temperature": 0.4}
    -> {"choices": [{"message": {"content": "..."}}]}

That is not inconsistency for its own sake. It is the one shape a large number
of services already speak -- OpenAI, OpenRouter, Together, Groq's compatible
endpoint, vLLM, LM Studio, LiteLLM, Ollama's `/v1` route -- so a buyer with any
of those needs to paste a URL and a key and nothing else. Inventing our own
envelope here would have bought consistency and cost every one of them.

--------------------------------------------------------------------------
SECURITY
--------------------------------------------------------------------------

1.  SERVER SIDE ONLY. Nothing in the browser ever calls a custom endpoint or
    ever learns its token. The dashboard posts the configuration to the
    settings service; the token is encrypted at rest by `secrets_store`; the
    request is made from this process. There is no code path from a page to an
    arbitrary URL, and there must never be one.

2.  https ONLY, and never to a private address. A user-supplied URL is a
    server-side request forgery risk: an endpoint of `http://169.254.169.254/`
    would hand cloud instance credentials to whoever typed it, and
    `http://localhost:8000/` would let the settings service call itself. Every
    resolved address is checked against the loopback, link-local, private and
    reserved ranges BEFORE the socket is opened, on every request rather than
    once at save time -- a hostname that resolved publicly when it was saved
    can resolve to 127.0.0.1 later, and checking only at save time is the
    classic DNS-rebinding hole. Plain http is refused outright, with one
    documented exception: an explicit loopback host is allowed when
    ALLOW_LOCAL_CUSTOM_ENDPOINTS is set, because a locally hosted model is a
    real use and the operator setting that variable is making an informed
    choice about their own machine.

3.  REDIRECTS ARE NOT FOLLOWED. A 302 to a private address would walk straight
    past the check above.

4.  TIMEOUTS AND SIZE CAPS ARE ENFORCED, not requested. A batch of hundreds of
    leads cannot wait on an endpoint that holds the connection open, and a
    response that streams gigabytes must not be read into memory.

5.  SECRETS ARE NEVER LOGGED. The token is put in a header and nowhere else;
    log lines carry the host and the status code. Provider error text is
    truncated and stripped of anything that looks like a credential before it
    reaches a message a user might see.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from src.integrations.email_reader import IncomingMessage
from src.integrations.email_sender import EmailSender, SendResult
from src.integrations.sheets_crm import CRMBackend
from src.providers.base import (
    Capability,
    DiscoveredBusiness,
    DiscoveryRequest,
    FoundEmail,
    ProviderContext,
    TranslatedText,
)
from src.providers.registry import ProviderSpec
from src.reliability import log
from src.settings import env, env_bool

#: Hard ceiling on one call. Not configurable by the user: a custom endpoint
#: that needs longer than this is not usable for a batch of hundreds of leads,
#: and letting a tenant raise it turns one slow endpoint into a stalled queue.
TIMEOUT_SECONDS = 20.0

#: Hard ceiling on a response body, in bytes. Read incrementally and abandoned
#: the moment it is exceeded, so an endpoint that streams forever costs this
#: much memory and no more.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Longest provider error text ever repeated back to a user.
MAX_ERROR_CHARS = 200


class CustomEndpointError(RuntimeError):
    """The endpoint could not answer. Message is safe to show a user."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


class CustomConfig:
    """
    One tenant's custom endpoint for one capability.

    Built from the non-secret `options` the resolver passes in plus the token
    from the encrypted store. The token is read here, held in memory for the
    life of the adapter, and never put anywhere else.
    """

    __slots__ = ("base_url", "auth_style", "token", "headers", "model", "capability")

    def __init__(
        self,
        *,
        base_url: str,
        capability: str,
        auth_style: str = "bearer",
        token: str = "",
        headers: dict[str, str] | None = None,
        model: str = "",
    ) -> None:
        self.base_url = (base_url or "").strip()
        self.capability = capability
        self.auth_style = (auth_style or "bearer").strip().lower() or "bearer"
        self.token = token or ""
        self.headers = headers or {}
        self.model = model or ""

    def __repr__(self) -> str:
        """
        Host and capability only.

        A `repr` that included the token would leak it into every traceback,
        every debugger session and every log line that formats an object.
        """
        return f"<CustomEndpoint {self.capability} host={_host_of(self.base_url)}>"


def _parse_headers(raw: Any) -> dict[str, str]:
    """
    Extra headers, from a JSON object the user typed.

    Anything unparseable is dropped with a warning rather than raising: a
    malformed header block should not stop an otherwise working endpoint, and
    the warning tells the operator why their header is not being sent. Header
    names are restricted to what a header name can actually be, so a stray
    newline cannot be used to inject a second header.
    """
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("custom endpoint: extra headers are not valid JSON; ignoring them")
            return {}
    if not isinstance(raw, dict):
        log.warning("custom endpoint: extra headers must be a JSON object; ignoring them")
        return {}

    clean: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            log.warning("custom endpoint: dropping header with an unusable name")
            continue
        if name.lower() in ("authorization", "host", "content-length"):
            log.warning("custom endpoint: %s cannot be overridden here; dropping it", name)
            continue
        text = str(value).replace("\r", "").replace("\n", "").strip()
        if text:
            clean[name] = text
    return clean


def config_from_options(
    capability: Capability | str, options: dict[str, Any], token: str = ""
) -> CustomConfig:
    """Build a config from a tenant's `providers.<capability>.settings` block."""
    name = capability.value if isinstance(capability, Capability) else str(capability)
    return CustomConfig(
        base_url=str(options.get("base_url", "") or ""),
        capability=name,
        auth_style=str(options.get("auth_style", "bearer") or "bearer"),
        token=token,
        headers=_parse_headers(options.get("headers")),
        model=str(options.get("model", "") or ""),
    )


# --------------------------------------------------------------------------- #
# URL validation
# --------------------------------------------------------------------------- #


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _is_blocked_address(address: str) -> bool:
    """
    True for any address a custom endpoint must never reach.

    Covers loopback (own services), link-local (169.254.169.254 is the cloud
    instance metadata endpoint on AWS, GCP and Azure alike), private ranges
    (everything else on the operator's network), and the reserved/multicast
    space. Checked for every resolved address, not just the first: a hostname
    with both a public and a private A record must be refused.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _allow_loopback() -> bool:
    """
    Whether a locally hosted endpoint is permitted.

    Off by default. An operator who runs their own model on the same machine
    turns it on deliberately, in a variable they set themselves, and is
    accepting that a URL typed into the dashboard can then reach localhost.
    """
    return env_bool("ALLOW_LOCAL_CUSTOM_ENDPOINTS", False)


def validate_url(url: str) -> str:
    """
    Check a URL and resolve it, or raise with a message safe to show a user.

    Returns the host that was validated, so the caller can log it. Called
    before every request, deliberately -- see the DNS-rebinding note in the
    module docstring.
    """
    url = (url or "").strip()
    if not url:
        raise CustomEndpointError("No endpoint URL is set for this connection.")

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise CustomEndpointError("That endpoint URL could not be read.") from None

    host = parts.hostname or ""
    if not host:
        raise CustomEndpointError("That endpoint URL has no host in it.")

    loopback_ok = _allow_loopback()
    if parts.scheme == "http":
        if not loopback_ok:
            raise CustomEndpointError(
                "The endpoint URL has to start with https:// so the key is not "
                "sent in the clear."
            )
    elif parts.scheme != "https":
        raise CustomEndpointError("The endpoint URL has to start with https://.")

    try:
        resolved = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror:
        raise CustomEndpointError(
            f"The address {host} could not be found. Check the URL."
        ) from None

    addresses = {info[4][0] for info in resolved}
    blocked = [a for a in addresses if _is_blocked_address(a)]
    if blocked and not loopback_ok:
        raise CustomEndpointError(
            f"{host} points at an address on this machine or this private "
            "network, which is not allowed for a custom endpoint."
        )

    return host


def _sanitise(text: str, config: CustomConfig) -> str:
    """
    Trim provider error text and make sure the token is not inside it.

    An endpoint that echoes the Authorization header back in its own error
    body would otherwise put the key straight into a log line and a toast.
    """
    clean = " ".join(str(text or "").split())
    if config.token and config.token in clean:
        clean = clean.replace(config.token, "***")
    if len(clean) > MAX_ERROR_CHARS:
        clean = clean[:MAX_ERROR_CHARS] + "..."
    return clean


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #


def _headers_for(config: CustomConfig) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": env("SCRAPER_USER_AGENT", "leadgen-agent/1.0"),
    }
    headers.update(config.headers)

    if config.token:
        if config.auth_style == "bearer":
            headers["Authorization"] = f"Bearer {config.token}"
        elif config.auth_style == "header":
            headers["X-API-Key"] = config.token
        # "query" is applied to the URL by the caller; "none" sends nothing.
    return headers


def _url_for(config: CustomConfig) -> str:
    if config.auth_style == "query" and config.token:
        separator = "&" if "?" in config.base_url else "?"
        return f"{config.base_url}{separator}api_key={urllib.parse.quote(config.token)}"
    return config.base_url


def post_json(config: CustomConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """
    One POST to the configured endpoint. Raises `CustomEndpointError` only.

    Every failure mode -- a bad URL, a private address, a timeout, a non-200, a
    body that is not JSON, a body that is too big -- comes back as that one
    exception with a message a user can act on. Callers above translate it into
    whatever "could not do this" means for their capability.
    """
    import httpx

    host = validate_url(config.base_url)

    try:
        with httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,     # see the module docstring
            trust_env=False,            # no ambient proxy for a user-typed URL
        ) as client:
            with client.stream(
                "POST",
                _url_for(config),
                json=payload,
                headers=_headers_for(config),
            ) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise CustomEndpointError(
                            "The endpoint sent back more data than this can handle."
                        )
                text = bytes(body).decode("utf-8", errors="replace")
                status = response.status_code
    except CustomEndpointError:
        raise
    except httpx.TimeoutException:
        log.warning("custom endpoint %s timed out after %.0fs", host, TIMEOUT_SECONDS)
        raise CustomEndpointError(
            f"{host} did not answer within {TIMEOUT_SECONDS:.0f} seconds."
        ) from None
    except Exception as exc:  # noqa: BLE001 - an http client can raise anything
        log.warning("custom endpoint %s failed: %s", host, exc.__class__.__name__)
        raise CustomEndpointError(f"{host} could not be reached.") from None

    if status >= 400:
        log.warning("custom endpoint %s answered %d", host, status)
        raise CustomEndpointError(
            f"{host} answered {status}. {_sanitise(text, config)}".strip()
        )

    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        log.warning("custom endpoint %s sent a body that is not JSON", host)
        raise CustomEndpointError(
            f"{host} answered with something that is not JSON."
        ) from None

    if parsed is None:
        return {}
    if isinstance(parsed, list):
        return {"results": parsed}
    if not isinstance(parsed, dict):
        raise CustomEndpointError(f"{host} answered with an unexpected shape.")
    return parsed


def call(
    config: CustomConfig, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """The standard envelope. See the table in the module docstring."""
    return post_json(
        config,
        {"capability": config.capability, "action": action, "payload": payload},
    )


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CustomLLM:
    """
    Any OpenAI-compatible chat-completions endpoint.

    Returns an `llm.LLMResponse` so N3/N4/N7 cannot tell the difference, and
    raises `llm.LLMUnavailable` on failure so `@node` routes the lead to manual
    review the same way it does for every other model failure.
    """

    id = "custom_llm"

    def __init__(self, config: CustomConfig) -> None:
        self._config = config

    def available(self) -> bool:
        return bool(self._config.base_url)

    def model_name(self) -> str:
        return self._config.model or "custom"

    def complete(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.4,
        context: dict | None = None,
    ) -> Any:
        from src.integrations.llm import LLMResponse, LLMUnavailable

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {"messages": messages, "temperature": temperature}
        if self._config.model:
            body["model"] = self._config.model

        try:
            answer = post_json(self._config, body)
        except CustomEndpointError as exc:
            raise LLMUnavailable(f"custom endpoint: {exc}") from None

        text = self._text_from(answer)
        if not text.strip():
            raise LLMUnavailable("the custom endpoint returned an empty answer")
        return LLMResponse(text=text, provider=self.id, model=self.model_name())

    @staticmethod
    def _text_from(answer: dict[str, Any]) -> str:
        """
        Pull the text out, tolerating the shapes seen in the wild.

        The chat-completions shape first, then the two flat shapes some
        self-hosted servers return. Being generous here costs nothing and saves
        a buyer from debugging a JSON path.
        """
        choices = answer.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    # Some servers return content blocks rather than a string.
                    if isinstance(content, list):
                        return "".join(
                            part.get("text", "") if isinstance(part, dict) else str(part)
                            for part in content
                        )
                if isinstance(first.get("text"), str):
                    return first["text"]
        for key in ("text", "content", "output", "response"):
            if isinstance(answer.get(key), str):
                return answer[key]
        return ""

    def complete_json(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.2,
        context: dict | None = None,
        required_keys: tuple[str, ...] = (),
    ) -> tuple[dict, Any]:
        """
        Ask for JSON, reusing the shared parser and its one blunt retry.

        The extraction and re-prompt logic lives in `integrations.llm` and is
        not duplicated here -- a custom endpoint wrapping a model that fences
        its JSON in markdown needs exactly the same handling as Groq doing it.
        """
        from src.integrations.llm import extract_json

        json_system = (
            (system + "\n\n" if system else "")
            + "Respond with a single valid JSON object and nothing else. "
              "No prose, no markdown code fences, no explanation."
        )
        response = self.complete(
            prompt, task=task, system=json_system,
            temperature=temperature, context=context,
        )
        try:
            parsed = extract_json(response.text)
            missing = [key for key in required_keys if key not in parsed]
            if missing:
                raise ValueError(f"response is missing required keys {missing}")
            return parsed, response
        except ValueError as exc:
            log.warning(
                "custom endpoint json parse failed for task=%s (%s); retrying once",
                task, exc,
            )

        response = self.complete(
            prompt + f"\n\nYour previous reply could not be parsed. Return ONLY a "
                     f"JSON object with these keys: {list(required_keys)}.",
            task=task, system=json_system, temperature=0.0, context=context,
        )
        parsed = extract_json(response.text)
        missing = [key for key in required_keys if key not in parsed]
        if missing:
            raise ValueError(f"response is still missing required keys {missing}")
        return parsed, response


class CustomDiscovery:
    """A tenant's own source of businesses or contacts."""

    def __init__(self, config: CustomConfig, kind: str) -> None:
        self.id = f"custom_discovery_{'local' if kind == 'local_business' else 'b2b'}"
        self.kind = kind
        self._config = config

    def available(self) -> bool:
        return bool(self._config.base_url)

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        payload = {
            "kind": request.kind,
            "search_terms": request.search_terms,
            "locations": request.locations,
            "titles": request.titles,
            "industries": request.industries,
            "employee_range": request.employee_range,
            "limit": request.limit,
            "niche_id": request.niche_id,
            "region": request.region,
        }
        try:
            answer = call(self._config, "find", payload)
        except CustomEndpointError as exc:
            # Discovery never raises. An empty target is reported as low yield
            # on the batch summary, where a human will see it.
            log.warning("custom discovery failed for niche=%s: %s", request.niche_id, exc)
            return []

        rows = answer.get("results")
        if not isinstance(rows, list):
            log.warning("custom discovery answered without a 'results' list")
            return []

        found: list[DiscoveredBusiness] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("company_name") or row.get("name") or "").strip()
            if not name:
                # A record with no business name cannot become a lead, and
                # silently inventing one would poison the dedupe ledger.
                continue
            found.append(
                DiscoveredBusiness(
                    company_name=name,
                    website=str(row.get("website") or ""),
                    address=str(row.get("address") or ""),
                    phone=str(row.get("phone") or ""),
                    category=str(row.get("category") or ""),
                    rating=_as_float(row.get("rating")),
                    review_count=_as_int(row.get("review_count")),
                    business_status=str(row.get("business_status") or ""),
                    contact_name=str(row.get("contact_name") or ""),
                    contact_email=str(row.get("contact_email") or row.get("email") or ""),
                    title=str(row.get("title") or ""),
                    linkedin_url=str(row.get("linkedin_url") or ""),
                    location=str(row.get("location") or ""),
                    industry=str(row.get("industry") or ""),
                    employee_count=_as_int(row.get("employee_count")),
                    source=str(row.get("source") or "custom"),
                )
            )
        limit = request.limit
        return found[:limit] if limit else found


class CustomEnrichment:
    """A tenant's own contact-lookup service."""

    id = "custom_enrichment"

    def __init__(self, config: CustomConfig) -> None:
        self._config = config

    def available(self) -> bool:
        return bool(self._config.base_url)

    def find_email(
        self, domain: str, *, first_name: str = "", last_name: str = ""
    ) -> FoundEmail | None:
        try:
            answer = call(
                self._config,
                "find_email",
                {"domain": domain, "first_name": first_name, "last_name": last_name},
            )
        except CustomEndpointError as exc:
            log.warning("custom enrichment failed for %s: %s", domain, exc)
            return None

        email = str(answer.get("email") or "").strip()
        if not email:
            return None
        return FoundEmail(
            email=email,
            confidence=_as_int(answer.get("confidence")) or 0,
            first_name=str(answer.get("first_name") or ""),
            last_name=str(answer.get("last_name") or ""),
            position=str(answer.get("position") or ""),
            source="custom",
        )

    def budget_remaining(self) -> int:
        # A tenant's own endpoint has no allowance this product can meter, so
        # N2 is told not to ration -- their endpoint, their limits.
        return 1_000_000

    def budget_status(self) -> str:
        return f"custom endpoint at {_host_of(self._config.base_url)}"


class CustomEmailSender(EmailSender):
    """
    A tenant's own sending service.

    Subclasses the real `EmailSender` rather than reimplementing it, so the
    durable daily counter, the budget check before the call and the
    consume-only-on-success rule all apply here identically. Nothing about
    "it is a custom endpoint" should mean a workspace can exceed its own
    daily send limit.
    """

    provider = "custom_email_sender"

    def __init__(
        self, tenant_id: str, config: CustomConfig, daily_limit: int | None = None
    ) -> None:
        super().__init__(tenant_id, daily_limit)
        self._config = config

    def _send(
        self, *, to: str, to_name: str, subject: str, body: str,
        from_name: str, from_email: str, reply_to: str,
    ) -> SendResult:
        try:
            answer = call(
                self._config,
                "send",
                {
                    "to": to, "to_name": to_name, "subject": subject, "body": body,
                    "from_name": from_name, "from_email": from_email,
                    "reply_to": reply_to,
                },
            )
        except CustomEndpointError as exc:
            return SendResult(ok=False, provider=self.provider, detail=str(exc))

        ok = bool(answer.get("ok", True))
        return SendResult(
            ok=ok,
            provider=self.provider,
            message_id=str(answer.get("message_id") or ""),
            detail=_sanitise(str(answer.get("detail") or ""), self._config),
        )


class CustomEmailReader:
    """A tenant's own reply feed."""

    def __init__(self, config: CustomConfig) -> None:
        self._config = config

    def fetch_replies(self, address: str, since: datetime) -> list[IncomingMessage]:
        try:
            answer = call(
                self._config,
                "fetch_replies",
                {"address": address, "since": since.isoformat()},
            )
        except CustomEndpointError as exc:
            # No replies found is a correct answer for a mailbox that cannot be
            # read. N7 leaves the lead in its sequence rather than guessing.
            log.warning("custom reply reader failed: %s", exc)
            return []

        rows = answer.get("messages")
        if not isinstance(rows, list):
            return []

        messages: list[IncomingMessage] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            messages.append(
                IncomingMessage(
                    from_address=str(row.get("from_address") or row.get("from") or ""),
                    subject=str(row.get("subject") or ""),
                    body=str(row.get("body") or ""),
                    received_at=_parse_when(row.get("received_at")),
                    message_id=str(row.get("message_id") or ""),
                    in_reply_to=str(row.get("in_reply_to") or ""),
                    raw_headers={
                        str(k).lower(): str(v)
                        for k, v in (row.get("headers") or {}).items()
                    }
                    if isinstance(row.get("headers"), dict)
                    else None,
                )
            )
        return messages


def _parse_when(value: Any) -> datetime:
    """
    An ISO 8601 timestamp, or now.

    Defaulting to now rather than to the epoch matters: N7 filters replies by
    `since`, and an epoch default would make every undated message look old
    enough to ignore.
    """
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            log.debug("custom reply reader sent an unreadable timestamp")
    return datetime.now(timezone.utc)


class CustomCRM(CRMBackend):
    """A tenant's own records endpoint."""

    def __init__(self, tenant_id: str, config: CustomConfig) -> None:
        super().__init__(tenant_id)
        self._config = config

    def upsert_lead(self, state: Any) -> str:
        answer = call(self._config, "upsert_lead", {"lead": dict(state)})
        if not bool(answer.get("ok", True)):
            raise CustomEndpointError(
                _sanitise(str(answer.get("detail") or "the endpoint refused the row"),
                          self._config)
            )
        ref = str(answer.get("ref") or "")
        return f"custom endpoint{f' ({ref})' if ref else ''}"

    def append_suppression(self, entry: dict[str, Any]) -> None:
        call(self._config, "append_suppression", {"entry": entry})


class CustomTranslation:
    """A tenant's own translation endpoint."""

    id = "custom_translation"

    def __init__(self, config: CustomConfig) -> None:
        self._config = config

    def available(self) -> bool:
        return bool(self._config.base_url)

    def translate(self, text: str, language: str) -> TranslatedText | None:
        from src.integrations.translation import nothing_to_do

        settled = nothing_to_do(text, language)
        if settled is not None:
            return TranslatedText(
                text=settled.text, language=settled.language,
                provider=settled.provider, translated=settled.translated,
            )

        try:
            answer = call(
                self._config, "translate", {"text": text.strip(), "language": language}
            )
        except CustomEndpointError as exc:
            log.warning("custom translation to %s failed: %s", language, exc)
            return None

        translated = str(answer.get("text") or "").strip()
        if not translated:
            return None
        return TranslatedText(
            text=translated,
            language=language,
            provider="custom",
            translated=bool(answer.get("translated", True)),
        )


# --------------------------------------------------------------------------- #
# Registry factory
# --------------------------------------------------------------------------- #


def build(spec: ProviderSpec, ctx: ProviderContext):
    """
    Build the custom adapter for one capability.

    The token comes from the encrypted store here, at the last possible
    moment, keyed on the provider id -- so it is the same write-only path as
    every other credential and never travels through the resolver.
    """
    from src.settings import credential

    token = credential(f"CUSTOM_TOKEN__{spec.capability.value}", ctx.tenant_id, "")
    if not token:
        token = str(ctx.option("token", "") or "")

    config = config_from_options(spec.capability, ctx.options, token)

    if spec.capability is Capability.LLM:
        return CustomLLM(config)
    if spec.capability is Capability.DISCOVERY_LOCAL:
        return CustomDiscovery(config, "local_business")
    if spec.capability is Capability.DISCOVERY_B2B:
        return CustomDiscovery(config, "b2b")
    if spec.capability is Capability.ENRICHMENT:
        return CustomEnrichment(config)
    if spec.capability is Capability.EMAIL_SENDER:
        limits = dict(ctx.option("daily_limits", {}) or {})
        return CustomEmailSender(
            ctx.tenant_id, config, limits.get("custom_email_sender")
        )
    if spec.capability is Capability.EMAIL_READER:
        return CustomEmailReader(config)
    if spec.capability is Capability.CRM:
        return CustomCRM(ctx.tenant_id, config)
    if spec.capability is Capability.TRANSLATION:
        return CustomTranslation(config)

    raise CustomEndpointError(
        f"there is no custom adapter for {spec.capability.value}"
    )
