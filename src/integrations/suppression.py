"""
suppression.py -- suppression list read / write / check.

Per-tenant suppression store, resolved STRICTLY by tenant_id. Exact-match
lookup on normalised email and on normalised LinkedIn URL.

Section 7 is non-negotiable here: tenant A's suppression entries must never be
visible to a tenant B lookup, and vice versa. That is enforced structurally --
`SuppressionList` has no cross-tenant read path at all, and the file path comes
from `settings.suppression_path(tenant_id)` which rejects a tenant_id that
could traverse out of its directory. tests/test_suppression_gate.py proves it
in both directions.

Two backends, both writing the same records:
  * `local` -- a JSON file per tenant. Always the source of truth for a check,
    because a send must never depend on a network call to Google succeeding.
  * `crm`   -- mirrored into a Suppression worksheet so the operator can see
    and edit the list without shell access.

`SUPPRESSION_BACKEND=both` (the default) writes to each and reads from local.
That asymmetry is deliberate: a Sheets outage must not silently make an
opted-out contact look contactable.

Env: SUPPRESSION_BACKEND, SUPPRESSION_DIR
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable

from src.reliability import log
from src.settings import env, suppression_path
from src.state import utcnow

#: Phrases in a reply that mean "stop contacting me", in the languages this
#: system localises into. Matched case-insensitively as substrings, so
#: "please UNSUBSCRIBE me" and "unsubscribe" both hit.
OPT_OUT_PHRASES: tuple[str, ...] = (
    # English
    "unsubscribe", "opt out", "opt-out", "remove me", "take me off",
    "do not contact", "don't contact", "do not email", "don't email",
    "stop emailing", "stop contacting", "no longer wish", "not interested",
    "delete my data", "delete my details", "remove my details",
    "remove from your list", "off your list", "cease all", "stop sending",
    # French
    "se désabonner", "desabonner", "ne plus recevoir", "ne me contactez plus",
    "retirez-moi", "supprimez mes données",
    # German
    "abmelden", "keine weiteren", "nicht kontaktieren", "austragen",
    "löschen sie meine daten",
    # Spanish / Italian / Dutch
    "darse de baja", "no recibir", "cancella iscrizione", "uitschrijven",
    # Arabic
    "إلغاء الاشتراك", "لا ترغب", "توقف عن",
)

#: Phrases that are specifically a legal / compliance challenge. These suppress
#: AND flag for human attention -- an unanswered GDPR question is a real risk,
#: not just a lost lead.
COMPLIANCE_OBJECTION_PHRASES: tuple[str, ...] = (
    "gdpr", "dsgvo", "rgpd", "ccpa", "can-spam", "casl", "pecr", "pdpl",
    "legal basis", "rechtsgrundlage", "base légale", "base legale",
    "where did you get my", "how did you get my", "wie sind sie an meine",
    "data protection", "datenschutz", "protection des données",
    "ico complaint", "report you", "unsolicited",
)


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def normalise_linkedin(url: str) -> str:
    """
    Reduce a LinkedIn URL to a stable key.

    linkedin.com URLs arrive with and without www, with and without a trailing
    slash, with tracking query strings, and occasionally with a locale prefix.
    All of those are the same person, and a suppression list that misses one
    spelling is worse than no list.
    """
    url = (url or "").strip().lower()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^([a-z]{2}\.)?(www\.)?linkedin\.com", "linkedin.com", url)
    url = url.split("?")[0].split("#")[0].rstrip("/")
    return url


def domain_of(email: str) -> str:
    email = normalise_email(email)
    return email.split("@", 1)[1] if "@" in email else ""


@dataclass(frozen=True)
class SuppressionEntry:
    email: str = ""
    linkedin_url: str = ""
    reason: str = ""
    source: str = ""          # reply | manual | compliance | import
    lead_id: str = ""
    added_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "email": self.email,
            "linkedin_url": self.linkedin_url,
            "reason": self.reason,
            "source": self.source,
            "lead_id": self.lead_id,
            "added_at": self.added_at,
        }


class SuppressionList:
    """
    One tenant's suppression list.

    Construct it with a tenant_id and it can only ever see that tenant's data.
    There is deliberately no `SuppressionList.all_tenants()` or any other
    aggregate accessor -- the class cannot be misused across tenants because
    the capability does not exist.
    """

    def __init__(self, tenant_id: str):
        if not tenant_id:
            raise ValueError("SuppressionList requires a tenant_id")
        self.tenant_id = tenant_id
        self.path = suppression_path(tenant_id)   # validates the tenant_id
        self._cache: dict[str, Any] | None = None

    # -- storage ------------------------------------------------------------ #

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("tenant_id", self.tenant_id)
        data.setdefault("emails", {})
        data.setdefault("linkedin_urls", {})
        data.setdefault("domains", {})
        if data.get("tenant_id") != self.tenant_id:
            # A file whose contents disagree with its filename is a bug or a
            # tampered copy. Refuse it rather than apply another tenant's list.
            raise ValueError(
                f"suppression file {self.path} declares tenant_id "
                f"{data['tenant_id']!r} but was loaded for {self.tenant_id!r}"
            )
        self._cache = data
        return data

    def _save(self, data: dict[str, Any]) -> None:
        data["tenant_id"] = self.tenant_id
        data["updated_at"] = utcnow().isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            from pathlib import Path

            Path(tmp).unlink(missing_ok=True)
            raise
        self._cache = data

    def reload(self) -> None:
        """Drop the in-memory cache. Called before each send-time check."""
        self._cache = None

    # -- checks ------------------------------------------------------------- #

    def is_suppressed(self, *, email: str = "", linkedin_url: str = "") -> tuple[bool, str]:
        """
        Exact-match check on email and LinkedIn URL. Returns (blocked, reason).

        Also checks the address's DOMAIN: when a company says "stop contacting
        anyone here", suppressing one mailbox and continuing to mail their
        colleague is the failure that gets a sending domain blacklisted.
        """
        data = self._load()

        key = normalise_email(email)
        if key and key in data["emails"]:
            entry = data["emails"][key]
            return True, f"email suppressed: {entry.get('reason', 'opted out')}"

        domain = domain_of(email)
        if domain and domain in data.get("domains", {}):
            entry = data["domains"][domain]
            return True, f"domain suppressed: {entry.get('reason', 'opted out')}"

        li = normalise_linkedin(linkedin_url)
        if li and li in data["linkedin_urls"]:
            entry = data["linkedin_urls"][li]
            return True, f"LinkedIn profile suppressed: {entry.get('reason', 'opted out')}"

        return False, ""

    def __contains__(self, value: str) -> bool:
        blocked, _ = self.is_suppressed(email=value, linkedin_url=value)
        return blocked

    def __len__(self) -> int:
        data = self._load()
        return len(data["emails"]) + len(data["linkedin_urls"]) + len(data.get("domains", {}))

    def entries(self) -> list[dict[str, Any]]:
        data = self._load()
        out: list[dict[str, Any]] = []
        for bucket in ("emails", "linkedin_urls", "domains"):
            out.extend(data.get(bucket, {}).values())
        return sorted(out, key=lambda e: e.get("added_at", ""), reverse=True)

    # -- mutation ----------------------------------------------------------- #

    def add(
        self,
        *,
        email: str = "",
        linkedin_url: str = "",
        domain: str = "",
        reason: str = "",
        source: str = "manual",
        lead_id: str = "",
    ) -> SuppressionEntry:
        """
        Add a contact to the list. Idempotent: re-adding an existing entry
        refreshes its reason rather than duplicating it.
        """
        entry = SuppressionEntry(
            email=normalise_email(email),
            linkedin_url=normalise_linkedin(linkedin_url),
            reason=reason or "opted out",
            source=source,
            lead_id=lead_id,
            added_at=utcnow().isoformat(),
        )
        data = self._load()
        if entry.email:
            data["emails"][entry.email] = entry.to_dict()
        if entry.linkedin_url:
            data["linkedin_urls"][entry.linkedin_url] = entry.to_dict()
        if domain:
            data.setdefault("domains", {})[domain.strip().lower()] = entry.to_dict()
        self._save(data)

        log.info(
            "suppressed tenant=%s email=%r linkedin=%r reason=%r source=%s",
            self.tenant_id, entry.email, entry.linkedin_url, entry.reason, source,
        )
        _mirror_to_crm(self.tenant_id, entry)
        return entry

    def add_many(self, entries: Iterable[dict[str, Any]]) -> int:
        count = 0
        for item in entries:
            self.add(**item)
            count += 1
        return count

    def remove(self, *, email: str = "", linkedin_url: str = "") -> bool:
        """
        Remove an entry. For correcting a mistake only.

        There is no bulk clear: an accidental "unsuppress everyone" is one of
        the few mistakes in this system that cannot be undone from the
        recipient's point of view.
        """
        data = self._load()
        removed = False
        key = normalise_email(email)
        if key and key in data["emails"]:
            del data["emails"][key]
            removed = True
        li = normalise_linkedin(linkedin_url)
        if li and li in data["linkedin_urls"]:
            del data["linkedin_urls"][li]
            removed = True
        if removed:
            self._save(data)
            log.warning(
                "UNSUPPRESSED tenant=%s email=%r linkedin=%r", self.tenant_id, key, li
            )
        return removed


# --------------------------------------------------------------------------- #
# CRM mirror
# --------------------------------------------------------------------------- #

def backend() -> str:
    return env("SUPPRESSION_BACKEND", "both").lower()


def _mirror_to_crm(tenant_id: str, entry: SuppressionEntry) -> None:
    """
    Best-effort mirror into the tenant's CRM suppression worksheet.

    Failures are logged and swallowed: the local file is the source of truth,
    and a Sheets outage must never stop a contact being suppressed.
    """
    if backend() not in ("crm", "both"):
        return
    try:
        from src.integrations import sheets_crm

        sheets_crm.append_suppression(tenant_id, entry.to_dict())
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "could not mirror suppression to CRM for tenant=%s (%s); "
            "the local list is still authoritative", tenant_id, exc,
        )


# --------------------------------------------------------------------------- #
# Reply analysis
# --------------------------------------------------------------------------- #

def contains_opt_out(text: str) -> bool:
    """True if `text` contains explicit opt-out language in any supported language."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in OPT_OUT_PHRASES)


def contains_compliance_objection(text: str) -> bool:
    """True if `text` raises a legal or data-protection challenge."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in COMPLIANCE_OBJECTION_PHRASES)


def for_tenant(tenant_id: str) -> SuppressionList:
    """The only way to obtain a suppression list. Always tenant-scoped."""
    return SuppressionList(tenant_id)
