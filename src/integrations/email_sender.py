"""
email_sender.py -- EmailSender interface.

One interface, three implementations selected in config:
  * GmailSMTPSender -- Gmail SMTP with an app password (~500/day ceiling)
  * BrevoSender     -- Brevo transactional API free plan (~300/day ceiling)
  * DryRunSender    -- prints what would be sent and sends nothing

Daily send counters are enforced PER TENANT PER PROVIDER by a durable counter
(src/counters.py), against the LOWER of the tenant's configured limit and the
provider's free-tier ceiling. A new sending domain that jumps to 200 messages a
day gets filtered, not delivered, so the tenant limit existing at all matters
more than the provider ceiling.

Brevo is called over its REST API with `requests` rather than its vendor SDK:
one HTTP POST does not justify a dependency.

Env: EMAIL_PROVIDER, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GMAIL_SMTP_HOST,
     GMAIL_SMTP_PORT, GMAIL_DAILY_SEND_CAP, BREVO_API_KEY, BREVO_SENDER_EMAIL,
     BREVO_SENDER_NAME, BREVO_DAILY_SEND_CAP
"""

from __future__ import annotations

import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import requests

from src.counters import Counter, QuotaExceeded, send_counter
from src.reliability import log, retry_once
from src.settings import daily_send_cap, env, env_int

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


class DailyLimitReached(RuntimeError):
    """
    The tenant's daily send budget is spent.

    N6a catches this and QUEUES the remaining sends for the next scheduled run
    rather than failing the batch -- an unsent message tomorrow is fine, a
    crashed batch is not.
    """


@dataclass
class SendResult:
    ok: bool
    provider: str
    message_id: str = ""
    detail: str = ""


class EmailSender(ABC):
    """What every sender must do. N6a only ever sees this interface."""

    provider = "abstract"

    def __init__(self, tenant_id: str, daily_limit: int | None = None):
        self.tenant_id = tenant_id
        ceiling = daily_send_cap(self.provider)
        self.daily_limit = min(daily_limit, ceiling) if daily_limit else ceiling

    @property
    def counter(self) -> Counter:
        counter = send_counter(self.tenant_id, self.provider)
        counter.cap = self.daily_limit          # tenant limit beats the ceiling
        return counter

    def remaining_today(self) -> int:
        return self.counter.remaining

    def check_budget(self) -> None:
        try:
            self.counter.check(1)
        except QuotaExceeded as exc:
            raise DailyLimitReached(str(exc)) from exc

    @abstractmethod
    def _send(self, *, to: str, to_name: str, subject: str, body: str,
              from_name: str, from_email: str, reply_to: str) -> SendResult:
        """Provider-specific send. Called only after the budget check passes."""

    def send(
        self, *, to: str, subject: str, body: str, from_name: str, from_email: str,
        to_name: str = "", reply_to: str = "",
    ) -> SendResult:
        """
        Send one message, enforcing the daily budget first and recording usage
        only on success. Counting before the send would leak budget on every
        transport failure; counting after means a message that actually went
        out is always counted.
        """
        self.check_budget()
        result = self._send(
            to=to, to_name=to_name, subject=subject, body=body,
            from_name=from_name, from_email=from_email, reply_to=reply_to,
        )
        if result.ok:
            used = self.counter.consume(1)
            log.info(
                "sent tenant=%s provider=%s to=%s (%d/%d today)",
                self.tenant_id, self.provider, to, used, self.daily_limit,
            )
        return result


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #

class DryRunSender(EmailSender):
    """
    Prints what would be sent. Records nothing against the daily counter --
    a dry run must not consume tomorrow's real budget.
    """

    provider = "dry_run"

    def __init__(self, tenant_id: str, daily_limit: int | None = None):
        self.tenant_id = tenant_id
        self.daily_limit = daily_limit or 9999
        self.sent: list[dict[str, str]] = []

    def check_budget(self) -> None:
        return

    def send(self, **kwargs) -> SendResult:            # type: ignore[override]
        return self._send(
            to=kwargs.get("to", ""), to_name=kwargs.get("to_name", ""),
            subject=kwargs.get("subject", ""), body=kwargs.get("body", ""),
            from_name=kwargs.get("from_name", ""), from_email=kwargs.get("from_email", ""),
            reply_to=kwargs.get("reply_to", ""),
        )

    def _send(self, *, to, to_name, subject, body, from_name, from_email, reply_to):
        self.sent.append({"to": to, "subject": subject, "body": body})
        print("\n" + "=" * 72)
        print("DRY RUN -- this email would be sent, and was NOT")
        print("=" * 72)
        print(f"From:    {from_name} <{from_email}>")
        print(f"To:      {to_name + ' ' if to_name else ''}<{to}>")
        if reply_to:
            print(f"Reply-To: {reply_to}")
        print(f"Subject: {subject}")
        print("-" * 72)
        print(body)
        print("=" * 72 + "\n")
        return SendResult(ok=True, provider="dry_run", message_id="dry-run", detail="printed")


# --------------------------------------------------------------------------- #
# Gmail SMTP
# --------------------------------------------------------------------------- #

class _SMTPSenderBase(EmailSender):
    """
    Everything two SMTP mailboxes have in common, which is nearly all of it.

    Subclasses answer one question -- where the host, port and credentials come
    from -- and inherit the message building, the TLS choice and the retry. The
    alternative was a second copy of this method differing in four lines, and a
    second copy is where the two quietly stop agreeing about, say, whether a
    Message-ID is set.
    """

    #: (host, port, username, password) for this mailbox.
    def _connection(self) -> tuple[str, int, str, str]:
        raise NotImplementedError

    def _send(self, *, to, to_name, subject, body, from_name, from_email, reply_to):
        host, port, username, password = self._connection()

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((from_name, from_email or username))
        message["To"] = formataddr((to_name, to)) if to_name else to
        if reply_to:
            message["Reply-To"] = reply_to
        message["Message-ID"] = make_msgid(domain=(from_email or username).split("@")[-1])
        message.set_content(body)

        def deliver() -> None:
            context = ssl.create_default_context()
            # 465 is implicit TLS from the first byte; everything else opens in
            # the clear and upgrades. Guessing wrong hangs rather than fails,
            # which is why the port decides rather than a setting.
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
                    server.login(username, password)
                    server.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=30) as server:
                    server.starttls(context=context)
                    server.login(username, password)
                    server.send_message(message)

        retry_once(deliver, _label=self.provider, _backoff_seconds=3.0)
        return SendResult(
            ok=True, provider=self.provider, message_id=message["Message-ID"],
        )


class GmailSMTPSender(_SMTPSenderBase):
    """
    Gmail SMTP with an app password.

    Requires 2FA on the account and an App Password -- NOT the login password.
    Gmail's own ~500/day ceiling applies to the account, not to this tenant, so
    two tenants sharing one Gmail account share that ceiling; the per-tenant
    counter here only stops each tenant exceeding ITS configured share.
    """

    provider = "gmail_smtp"

    def _connection(self) -> tuple[str, int, str, str]:
        address = env("GMAIL_ADDRESS")
        password = env("GMAIL_APP_PASSWORD")
        if not address or not password:
            raise RuntimeError(
                "GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set. Enable 2FA on "
                "the account, then create an App Password at "
                "myaccount.google.com -> Security -> App passwords."
            )
        return (
            env("GMAIL_SMTP_HOST", "smtp.gmail.com"),
            env_int("GMAIL_SMTP_PORT", 587),
            address,
            password,
        )


class SMTPSender(_SMTPSenderBase):
    """
    Any other mailbox: a work address, a hosting account, a transactional
    relay, a self-hosted server.

    No provider-specific behaviour at all, which is the feature. The daily cap
    is still enforced by the counter in EmailSender -- an SMTP server that
    would happily accept ten thousand messages is exactly the case where a
    self-imposed ceiling matters, because a new sending domain that jumps to
    hundreds a day gets filtered rather than delivered.
    """

    provider = "smtp"

    def _connection(self) -> tuple[str, int, str, str]:
        host = env("SMTP_HOST")
        username = env("SMTP_USERNAME")
        password = env("SMTP_PASSWORD")
        if not host or not username or not password:
            raise RuntimeError(
                "SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD must all be set to "
                "send through your own mail server."
            )
        return host, env_int("SMTP_PORT", 587), username, password


# --------------------------------------------------------------------------- #
# Brevo
# --------------------------------------------------------------------------- #

class BrevoSender(EmailSender):
    """Brevo (Sendinblue) transactional API, free plan."""

    provider = "brevo"

    def _send(self, *, to, to_name, subject, body, from_name, from_email, reply_to):
        api_key = env("BREVO_API_KEY")
        if not api_key:
            raise RuntimeError("BREVO_API_KEY must be set to send through Brevo.")

        sender_email = env("BREVO_SENDER_EMAIL") or from_email
        payload = {
            "sender": {
                "name": env("BREVO_SENDER_NAME") or from_name,
                "email": sender_email,
            },
            "to": [{"email": to, **({"name": to_name} if to_name else {})}],
            "subject": subject,
            "textContent": body,
        }
        if reply_to:
            payload["replyTo"] = {"email": reply_to}

        def deliver():
            response = requests.post(
                BREVO_SEND_URL,
                headers={
                    "api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=30,
            )
            if response.status_code not in (200, 201, 202):
                raise RuntimeError(
                    f"Brevo returned {response.status_code}: {response.text[:300]}"
                )
            return response.json()

        data = retry_once(deliver, _label="brevo", _backoff_seconds=3.0)
        return SendResult(
            ok=True, provider=self.provider, message_id=str(data.get("messageId", "")),
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_sender(
    tenant_id: str,
    *,
    provider: str = "",
    daily_limits: dict[str, int] | None = None,
    dry_run: bool = False,
) -> EmailSender:
    """
    Resolve the sender for one tenant.

    A dry run always gets DryRunSender, and so does a run whose credentials are
    missing -- printing what would have been sent is the correct degradation,
    and it means a fresh clone can exercise N6a with an empty .env.
    """
    provider = (provider or env("EMAIL_PROVIDER", "gmail_smtp")).lower()
    limit = (daily_limits or {}).get(provider)

    if dry_run:
        return DryRunSender(tenant_id, limit)

    if provider == "brevo":
        if env("BREVO_API_KEY"):
            return BrevoSender(tenant_id, limit)
        log.warning("BREVO_API_KEY is not set; falling back to a dry-run sender")
        return DryRunSender(tenant_id, limit)

    if provider == "smtp":
        if env("SMTP_HOST") and env("SMTP_USERNAME") and env("SMTP_PASSWORD"):
            return SMTPSender(tenant_id, limit)
        log.warning("SMTP is not fully configured; falling back to a dry-run sender")
        return DryRunSender(tenant_id, limit)

    if env("GMAIL_ADDRESS") and env("GMAIL_APP_PASSWORD"):
        return GmailSMTPSender(tenant_id, limit)

    log.warning(
        "Gmail SMTP credentials are not set; falling back to a dry-run sender "
        "(nothing will actually be sent)"
    )
    return DryRunSender(tenant_id, limit)
