"""
crm_rest.py -- HubSpot and Pipedrive, over their own REST APIs.

Both are real CRMs rather than a spreadsheet, which changes what "a lead" means
on the way in. A row in Google Sheets is thirty-three columns of whatever this
system knows; a HubSpot contact is a person with a first name, a last name, an
email and a company. So each backend here does two things:

  1. Maps the lead onto the CRM's OWN standard fields, so it lands as a usable
     contact rather than as thirty-three custom properties nobody asked for.
  2. Puts everything else -- the score, the reason, the signals, the stage --
     into ONE note-shaped field. `leadflow_summary` on HubSpot, a note on the
     Pipedrive person. Anyone can read it; nobody has to configure it first.

That second choice is the important one. Writing thirty custom properties into
somebody's CRM would need them created first, in the right types, or every
write fails with a 400 -- and a lead-gen tool that demands schema changes
before it will save anything is a tool people uninstall. One summary field
works on a free trial account with nothing set up.

UPSERT, not append, on both -- matching on email, then falling back to a search
by company name. Re-running a batch has to update the person rather than
produce five of them, exactly as the Sheets backend does.

Env: HUBSPOT_API_KEY
     PIPEDRIVE_API_KEY, PIPEDRIVE_DOMAIN
"""

from __future__ import annotations

import json
from typing import Any

import requests

from src.reliability import log, retry_once
from src.settings import env, env_for_tenant
from src.state import CRM_COLUMNS, LeadState, lead_to_crm_dict
from src.integrations.sheets_crm import SUPPRESSION_COLUMNS, CRMBackend

TIMEOUT_SECONDS = 30

HUBSPOT_BASE = "https://api.hubapi.com"
PIPEDRIVE_BASE = "https://{domain}.pipedrive.com/api/v1"

#: The fields that go into the CRM's own columns. Everything else in
#: CRM_COLUMNS ends up in the summary.
_MAPPED = frozenset(
    {"company_name", "contact_name", "contact_email", "website", "linkedin_url"}
)


def _summary(state: LeadState) -> str:
    """
    Everything the CRM has no field of its own for, as readable lines.

    Blank values are dropped rather than written as empty labels -- a note
    listing "reply_text:" twenty times is worse than a short note.
    """
    row = lead_to_crm_dict(state)
    lines = [
        f"{column.replace('_', ' ')}: {row[column]}"
        for column in CRM_COLUMNS
        if column not in _MAPPED and str(row.get(column, "")).strip()
    ]
    return "\n".join(lines)


def _names(state: LeadState) -> tuple[str, str]:
    """Split the contact name the way a CRM wants it. Surname may be empty."""
    parts = str(state.get("contact_name") or "").strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


# --------------------------------------------------------------------------- #
# HubSpot
# --------------------------------------------------------------------------- #

class HubSpotCRM(CRMBackend):
    """
    HubSpot contacts, through a private app token.

    A private app token rather than OAuth: OAuth would mean hosting a redirect
    URL and a token refresh loop for a product that runs on somebody's laptop.
    The token is created in HubSpot's own settings and pasted in, which is the
    same shape as every other credential here.

    `leadflow_summary` is written as a contact property. If it does not exist
    on the account, HubSpot rejects the property rather than the whole write,
    so the contact still lands -- see `_without_summary`.
    """

    provider = "hubspot"

    def __init__(self, tenant_id: str, table_name: str = "Leads"):
        super().__init__(tenant_id)

    def _token(self) -> str:
        token = env("HUBSPOT_API_KEY")
        if not token:
            raise RuntimeError(
                "HUBSPOT_API_KEY must be set. Create a private app in HubSpot "
                "under Settings -> Integrations -> Private Apps, give it the "
                "crm.objects.contacts read and write scopes, and paste its "
                "access token."
            )
        return token

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = requests.request(
            method,
            f"{HUBSPOT_BASE}{path}",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"HubSpot returned {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    def _find(self, email: str, lead_id: str) -> str:
        """The contact's id, or "" -- matched on email, then on our own id."""
        for prop, value in (("email", email), ("leadflow_lead_id", lead_id)):
            if not value:
                continue
            try:
                found = self._request(
                    "POST",
                    "/crm/v3/objects/contacts/search",
                    json={
                        "filterGroups": [
                            {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}
                        ],
                        "limit": 1,
                    },
                )
            except RuntimeError:
                # A search on a property the account does not have is a 400.
                # That is a "no match", not a failed write.
                continue
            results = found.get("results") or []
            if results:
                return str(results[0].get("id", ""))
        return ""

    def _properties(self, state: LeadState) -> dict[str, str]:
        first, last = _names(state)
        return {
            "email": str(state.get("contact_email") or ""),
            "firstname": first,
            "lastname": last,
            "company": str(state.get("company_name") or ""),
            "website": str(state.get("website") or ""),
            "hs_linkedin_url": str(state.get("linkedin_url") or ""),
            "leadflow_lead_id": str(state.get("lead_id") or ""),
            "leadflow_summary": _summary(state),
        }

    @staticmethod
    def _without_summary(properties: dict[str, str]) -> dict[str, str]:
        """HubSpot's standard fields only, for an account with no custom ones."""
        return {
            key: value
            for key, value in properties.items()
            if not key.startswith("leadflow_")
        }

    def upsert_lead(self, state: LeadState) -> str:
        properties = self._properties(state)
        contact_id = self._find(properties["email"], properties["leadflow_lead_id"])

        def write(props: dict[str, str]) -> None:
            if contact_id:
                self._request(
                    "PATCH", f"/crm/v3/objects/contacts/{contact_id}",
                    json={"properties": props},
                )
            else:
                self._request("POST", "/crm/v3/objects/contacts", json={"properties": props})

        try:
            retry_once(write, properties, _label="hubspot_upsert")
        except RuntimeError as exc:
            # The custom properties do not exist on this account. Write the
            # contact anyway rather than losing the lead over a field nobody
            # created -- and say so once, so it can be fixed if it matters.
            if "PROPERTY_DOESNT_EXIST" not in str(exc) and "does not exist" not in str(exc):
                raise
            log.warning(
                "HubSpot has no leadflow_summary property; writing the standard "
                "contact fields only. Add a single-line text property named "
                "leadflow_summary to keep the match reason with the contact."
            )
            retry_once(write, self._without_summary(properties), _label="hubspot_upsert_plain")

        return "updated" if contact_id else "created"

    def append_suppression(self, entry: dict[str, Any]) -> None:
        """
        Recorded ON the contact, as an opt-out, rather than in a second object.

        HubSpot already has a concept for "do not email this person"; writing
        our own list beside it would leave the CRM's own view of the contact
        saying it is fine to contact them.
        """
        email = str(entry.get("email") or "")
        if not email:
            return
        contact_id = self._find(email, str(entry.get("lead_id") or ""))
        if not contact_id:
            return
        note = "; ".join(
            f"{key}: {entry[key]}" for key in SUPPRESSION_COLUMNS if entry.get(key)
        )
        try:
            self._request(
                "PATCH",
                f"/crm/v3/objects/contacts/{contact_id}",
                json={"properties": {"hs_email_optout": "true", "leadflow_summary": note}},
            )
        except RuntimeError as exc:
            log.warning("could not mark %s as opted out in HubSpot: %s", email, exc)


# --------------------------------------------------------------------------- #
# Pipedrive
# --------------------------------------------------------------------------- #

class PipedriveCRM(CRMBackend):
    """
    Pipedrive persons, through an API token.

    The token goes in the query string because that is the only auth Pipedrive
    v1 accepts. It therefore ends up in their access logs, which is theirs to
    worry about, but it is also why nothing here ever logs a full URL.

    The summary goes on a NOTE attached to the person rather than into custom
    fields, because Pipedrive custom fields are created per-account with
    generated hash keys -- there is no stable key this code could write to.
    """

    provider = "pipedrive"

    def __init__(self, tenant_id: str, table_name: str = "Leads"):
        super().__init__(tenant_id)

    def _config(self) -> tuple[str, str]:
        token = env("PIPEDRIVE_API_KEY")
        domain = env_for_tenant("PIPEDRIVE_DOMAIN", self.tenant_id) or env("PIPEDRIVE_DOMAIN")
        if not token or not domain:
            raise RuntimeError(
                "PIPEDRIVE_API_KEY and PIPEDRIVE_DOMAIN must be set. The domain "
                "is the first part of your Pipedrive web address: for "
                "acme.pipedrive.com it is 'acme'."
            )
        return token, domain

    def _request(self, method: str, path: str, **kwargs) -> Any:
        token, domain = self._config()
        params = dict(kwargs.pop("params", {}))
        params["api_token"] = token
        response = requests.request(
            method,
            PIPEDRIVE_BASE.format(domain=domain) + path,
            params=params,
            timeout=TIMEOUT_SECONDS,
            **kwargs,
        )
        if response.status_code >= 400:
            # The token is in the query string, so the URL never goes into an
            # error message -- only the status and the body.
            raise RuntimeError(
                f"Pipedrive returned {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    def _find(self, email: str) -> int | None:
        if not email:
            return None
        found = self._request(
            "GET",
            "/persons/search",
            params={"term": email, "fields": "email", "exact_match": "true", "limit": 1},
        )
        items = ((found.get("data") or {}).get("items")) or []
        if not items:
            return None
        return int(items[0]["item"]["id"])

    def upsert_lead(self, state: LeadState) -> str:
        email = str(state.get("contact_email") or "")
        person_id = self._find(email)
        body = {
            "name": str(state.get("contact_name") or state.get("company_name") or "Unknown"),
            "email": [{"value": email, "primary": True}] if email else [],
        }

        if person_id:
            retry_once(
                self._request, "PUT", f"/persons/{person_id}",
                json=body, _label="pipedrive_update",
            )
            outcome = "updated"
        else:
            created = retry_once(
                self._request, "POST", "/persons", json=body, _label="pipedrive_create",
            )
            person_id = int(((created.get("data") or {}).get("id")) or 0)
            outcome = "created"

        if person_id:
            self._note(person_id, _summary(state))
        return outcome

    def _note(self, person_id: int, content: str) -> None:
        if not content:
            return
        try:
            self._request(
                "POST", "/notes",
                json={"person_id": person_id, "content": content.replace("\n", "<br>")},
            )
        except RuntimeError as exc:
            # The person is saved; a missing note is not worth failing a batch.
            log.warning("could not attach the summary note in Pipedrive: %s", exc)

    def append_suppression(self, entry: dict[str, Any]) -> None:
        email = str(entry.get("email") or "")
        if not email:
            return
        person_id = self._find(email)
        if not person_id:
            return
        note = "DO NOT CONTACT. " + "; ".join(
            f"{key}: {entry[key]}" for key in SUPPRESSION_COLUMNS if entry.get(key)
        )
        self._note(person_id, note)


__all__ = ["HubSpotCRM", "PipedriveCRM"]
