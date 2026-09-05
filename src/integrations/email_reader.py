"""
email_reader.py -- EmailReader interface.

One interface, three implementations:
  * GmailAPIReader -- Gmail API over OAuth (token cached on disk)
  * IMAPReader     -- plain IMAP polling, works with the same Gmail app password
                      used for sending
  * NullReader     -- returns nothing, for dry runs and unconfigured installs

Both real readers search for messages FROM a specific address AFTER a given
timestamp, which is exactly the query N7 needs (`contact_email` + `sent_at`
window) and nothing more. Fetching a whole inbox and filtering in Python would
be slower and would pull in mail that has nothing to do with the campaign.

Env: EMAIL_READER, GMAIL_OAUTH_CLIENT_SECRETS_FILE, GMAIL_OAUTH_TOKEN_FILE,
     IMAP_HOST, IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD, REPLY_LOOKBACK_DAYS
"""

from __future__ import annotations

import base64
import email
import imaplib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from pathlib import Path

from src.reliability import log, retry_once
from src.settings import ROOT, env, env_int
from src.state import utcnow

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

#: Quoted-reply separators. Everything from the first match onward is our own
#: outgoing message quoted back at us -- classifying that would be classifying
#: our own copy.
_QUOTE_MARKERS = (
    "\n-----original message-----",
    "\n________________________________",
    "\nfrom: ",
    "\nsent: ",
    "\non ",
    "\nwrote:",
    "\n> ",
)


@dataclass
class IncomingMessage:
    """One received message, provider-agnostic."""

    from_address: str
    subject: str
    body: str
    received_at: datetime
    message_id: str = ""
    in_reply_to: str = ""
    raw_headers: dict[str, str] | None = None

    @property
    def auto_submitted(self) -> bool:
        """
        RFC 3834's `Auto-Submitted` header. When a mail system sets it, the
        message is definitively machine-generated -- far more reliable than
        pattern-matching an out-of-office body, so N7 checks it first.
        """
        headers = self.raw_headers or {}
        value = (headers.get("auto-submitted") or "").lower()
        if value and value != "no":
            return True
        for key in ("x-autoreply", "x-autorespond", "x-auto-response-suppress"):
            if headers.get(key):
                return True
        return (headers.get("precedence") or "").lower() in ("bulk", "auto_reply", "junk")


def strip_quoted_reply(body: str) -> str:
    """
    Keep only what the person actually wrote.

    Their real reply is above the quoted thread; the quote below is our own
    outgoing copy. Classifying the whole blob would find our own opt-out line
    and our own signals in every reply.
    """
    text = (body or "").replace("\r\n", "\n")
    lowered = text.lower()
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        index = lowered.find(marker)
        if index > 0:
            cut = min(cut, index)
    return text[:cut].strip()


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


def _extract_address(from_header: str) -> str:
    match = re.search(r"<([^>]+)>", from_header or "")
    if match:
        return match.group(1).strip().lower()
    return (from_header or "").strip().lower()


def _body_from_message(message: email.message.Message) -> str:
    """Prefer text/plain; fall back to stripping tags from text/html."""
    plain, html = "", ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html:
                html = text
    else:
        payload = message.get_payload(decode=True)
        charset = message.get_content_charset() or "utf-8"
        if payload:
            plain = payload.decode(charset, errors="replace")

    if plain:
        return plain
    if html:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return ""


class EmailReader(ABC):
    """What every reader must do. N7 only ever sees this interface."""

    provider = "abstract"

    @abstractmethod
    def fetch_replies(
        self, from_address: str, since: datetime, limit: int = 10
    ) -> list[IncomingMessage]:
        """Messages from `from_address` received after `since`, newest first."""


class NullReader(EmailReader):
    """No mailbox configured. Returns nothing rather than failing a batch."""

    provider = "none"

    def fetch_replies(self, from_address, since, limit=10):
        return []


class IMAPReader(EmailReader):
    """
    Plain IMAP polling.

    Works with the same Gmail app password used for sending, which makes it the
    lowest-setup option: no OAuth consent screen, no token file.
    """

    provider = "imap"

    def fetch_replies(self, from_address, since, limit=10):
        host = env("IMAP_HOST", "imap.gmail.com")
        port = env_int("IMAP_PORT", 993)
        username = env("IMAP_USERNAME") or env("GMAIL_ADDRESS")
        password = env("IMAP_PASSWORD") or env("GMAIL_APP_PASSWORD")
        if not username or not password:
            raise RuntimeError(
                "IMAP_USERNAME and IMAP_PASSWORD (or GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD) must be set to poll for replies."
            )

        def poll() -> list[IncomingMessage]:
            messages: list[IncomingMessage] = []
            connection = imaplib.IMAP4_SSL(host, port)
            try:
                connection.login(username, password)
                connection.select("INBOX", readonly=True)
                # IMAP SINCE has date granularity only, so widen by a day and
                # filter precisely in Python afterwards.
                since_date = (since - timedelta(days=1)).strftime("%d-%b-%Y")
                status, data = connection.search(
                    None, "FROM", f'"{from_address}"', "SINCE", since_date
                )
                if status != "OK":
                    return []
                ids = (data[0] or b"").split()[-limit:]
                for message_id in reversed(ids):
                    status, payload = connection.fetch(message_id, "(RFC822)")
                    if status != "OK" or not payload or not payload[0]:
                        continue
                    parsed = email.message_from_bytes(payload[0][1])
                    received = _parse_date(parsed.get("Date"))
                    if received < since:
                        continue
                    messages.append(
                        IncomingMessage(
                            from_address=_extract_address(_decode(parsed.get("From"))),
                            subject=_decode(parsed.get("Subject")),
                            body=_body_from_message(parsed),
                            received_at=received,
                            message_id=parsed.get("Message-ID", ""),
                            in_reply_to=parsed.get("In-Reply-To", ""),
                            raw_headers={k.lower(): v for k, v in parsed.items()},
                        )
                    )
            finally:
                try:
                    connection.logout()
                except Exception:  # noqa: BLE001
                    pass
            return messages

        return retry_once(poll, _label="imap_poll", _backoff_seconds=2.0)


class GmailAPIReader(EmailReader):
    """
    Gmail API over OAuth.

    Preferable to IMAP for volume (proper search, no full-message downloads),
    but it needs a one-time consent flow, so IMAP stays the default in
    .env.example.
    """

    provider = "gmail_api"

    def _service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        token_path = _resolve(env("GMAIL_OAUTH_TOKEN_FILE", "./.secrets/gmail_token.json"))
        secrets_path = _resolve(
            env("GMAIL_OAUTH_CLIENT_SECRETS_FILE", "./.secrets/gmail_client_secret.json")
        )

        credentials = None
        if token_path.exists():
            credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                if not secrets_path.exists():
                    raise RuntimeError(
                        f"Gmail OAuth client secrets not found at {secrets_path}. "
                        "Create an OAuth client (Desktop app) in Google Cloud with "
                        "the Gmail API enabled and download the JSON there."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(secrets_path), GMAIL_SCOPES
                )
                # Interactive once; the token file is reused afterwards, which
                # is what lets a scheduled run work unattended.
                credentials = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json(), encoding="utf-8")

        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def fetch_replies(self, from_address, since, limit=10):
        def poll() -> list[IncomingMessage]:
            service = self._service()
            query = f"from:{from_address} after:{int(since.timestamp())}"
            listing = service.users().messages().list(
                userId="me", q=query, maxResults=limit
            ).execute()

            messages: list[IncomingMessage] = []
            for stub in listing.get("messages", []):
                raw = service.users().messages().get(
                    userId="me", id=stub["id"], format="raw"
                ).execute()
                parsed = email.message_from_bytes(
                    base64.urlsafe_b64decode(raw["raw"].encode("ASCII"))
                )
                received = _parse_date(parsed.get("Date"))
                if received < since:
                    continue
                messages.append(
                    IncomingMessage(
                        from_address=_extract_address(_decode(parsed.get("From"))),
                        subject=_decode(parsed.get("Subject")),
                        body=_body_from_message(parsed),
                        received_at=received,
                        message_id=parsed.get("Message-ID", ""),
                        in_reply_to=parsed.get("In-Reply-To", ""),
                        raw_headers={k.lower(): v for k, v in parsed.items()},
                    )
                )
            return messages

        return retry_once(poll, _label="gmail_api_poll", _backoff_seconds=2.0)


def _resolve(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _parse_date(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def get_reader(*, provider: str = "", dry_run: bool = False) -> EmailReader:
    """
    Resolve the reader. Missing credentials degrade to NullReader rather than
    failing: no replies found is a correct answer for an unconfigured mailbox,
    and a batch must still be able to run its other nodes.
    """
    if dry_run:
        return NullReader()

    provider = (provider or env("EMAIL_READER", "imap")).lower()
    if provider == "gmail_api":
        return GmailAPIReader()
    if env("IMAP_USERNAME") or env("GMAIL_ADDRESS"):
        return IMAPReader()

    log.info("no mailbox configured; reply monitoring is inactive")
    return NullReader()
