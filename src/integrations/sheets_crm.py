"""
sheets_crm.py -- CRM backend interface.

One interface, five implementations, resolved STRICTLY by tenant_id (one
spreadsheet, base or account per tenant, never shared):

  * GoogleSheetsCRM -- gspread with a service account (primary)
  * AirtableCRM     -- Airtable free plan (alternate)
  * HubSpotCRM      -- HubSpot contacts, private app token   (crm_rest.py)
  * PipedriveCRM    -- Pipedrive persons, API token          (crm_rest.py)
  * DryRunCRM       -- writes a local CSV instead, used by --dry-run and by any
                       run where credentials are absent

The two real CRMs live in `crm_rest.py` rather than here, because they do
something the spreadsheet backends do not: they map a lead onto the CRM's own
idea of a contact, and put everything else in one summary field. That mapping
is the whole of those classes and it has nothing to do with sheets.

Columns follow state.CRM_COLUMNS exactly, so a Looker Studio report can be
pointed at the sheet with no transformation layer: clean headers, one row per
lead, enums as plain strings, ISO-8601 timestamps.

Rows are UPSERTED on lead_id rather than appended, so re-running a batch
updates a lead's row instead of producing five rows for one company and
breaking every count in the dashboard.

Env: CRM_BACKEND, GOOGLE_SERVICE_ACCOUNT_FILE,
     GOOGLE_SHEETS_SPREADSHEET_ID__<tenant>, AIRTABLE_API_KEY,
     AIRTABLE_BASE_ID__<tenant>, AIRTABLE_TABLE_NAME,
     HUBSPOT_API_KEY, PIPEDRIVE_API_KEY, PIPEDRIVE_DOMAIN__<tenant>
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from src.reliability import log, retry_once
from src.settings import ROOT, dry_run_output_path, env, env_for_tenant
from src.state import CRM_COLUMNS, LeadState, lead_to_crm_dict, lead_to_crm_row

SUPPRESSION_COLUMNS: tuple[str, ...] = (
    "email", "linkedin_url", "reason", "source", "lead_id", "added_at",
)


class CRMBackend(ABC):
    """What every CRM backend must do. Nodes only ever see this interface."""

    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    @abstractmethod
    def upsert_lead(self, state: LeadState) -> str:
        """Write or update this lead's row. Returns a short description."""

    @abstractmethod
    def append_suppression(self, entry: dict[str, Any]) -> None:
        """Mirror one suppression entry."""

    def upsert_many(self, leads: list[LeadState]) -> int:
        count = 0
        for lead in leads:
            try:
                self.upsert_lead(lead)
                count += 1
            except Exception as exc:  # noqa: BLE001
                # Section 8: one lead's CRM failure must not lose the other 39.
                log.error(
                    "CRM write failed tenant=%s lead=%s: %s",
                    self.tenant_id, lead.get("lead_id"), exc,
                )
        return count


# --------------------------------------------------------------------------- #
# Dry run / no-credentials backend
# --------------------------------------------------------------------------- #

class DryRunCRM(CRMBackend):
    """
    Writes a local CSV with the exact CRM_COLUMNS header.

    Useful beyond dry runs: the file can be uploaded to Sheets by hand, so an
    operator without a service account still gets a working Looker source.
    """

    def __init__(self, tenant_id: str, path: Path | None = None):
        super().__init__(tenant_id)
        self.path = path or dry_run_output_path(tenant_id)

    def _rows(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                next(reader)
            except StopIteration:
                return {}
            return {row[0]: row for row in reader if row}

    def upsert_lead(self, state: LeadState) -> str:
        rows = self._rows()
        row = lead_to_crm_row(state)
        rows[row[0]] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(CRM_COLUMNS)
            writer.writerows(rows.values())
        return f"csv:{self.path.name}"

    def append_suppression(self, entry: dict[str, Any]) -> None:
        path = self.path.with_name(self.path.stem + "__suppression.csv")
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUPPRESSION_COLUMNS)
            if not exists:
                writer.writeheader()
            writer.writerow({k: entry.get(k, "") for k in SUPPRESSION_COLUMNS})


# --------------------------------------------------------------------------- #
# Google Sheets
# --------------------------------------------------------------------------- #

class GoogleSheetsCRM(CRMBackend):
    """
    gspread with a service account.

    The spreadsheet id is read per tenant from the environment
    (`GOOGLE_SHEETS_SPREADSHEET_ID__<tenant_id>`) rather than from the tenant
    YAML, so no document id is committed to the repository and two tenants
    cannot end up sharing a sheet through a copy-pasted config.
    """

    def __init__(self, tenant_id: str, worksheet_name: str = "Leads",
                 suppression_worksheet: str = "Suppression"):
        super().__init__(tenant_id)
        self.worksheet_name = worksheet_name
        self.suppression_worksheet = suppression_worksheet
        self._client = None
        self._sheet = None

    @property
    def spreadsheet_id(self) -> str:
        value = env_for_tenant("GOOGLE_SHEETS_SPREADSHEET_ID", self.tenant_id)
        if not value:
            raise RuntimeError(
                f"no spreadsheet configured for tenant '{self.tenant_id}'. Set "
                f"GOOGLE_SHEETS_SPREADSHEET_ID__{self.tenant_id} in .env and share "
                "the sheet with your service account email as an Editor."
            )
        return value

    def _open(self):
        if self._sheet is not None:
            return self._sheet
        import gspread

        credentials_file = env(
            "GOOGLE_SERVICE_ACCOUNT_FILE", "./.secrets/google_service_account.json"
        )
        path = Path(credentials_file)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise RuntimeError(
                f"Google service account file not found at {path}. Create one in "
                "Google Cloud (Sheets API + Drive API enabled), download the JSON "
                "key, and point GOOGLE_SERVICE_ACCOUNT_FILE at it."
            )
        self._client = gspread.service_account(filename=str(path))
        self._sheet = self._client.open_by_key(self.spreadsheet_id)
        return self._sheet

    def _worksheet(self, name: str, header: tuple[str, ...]):
        import gspread

        sheet = self._open()
        try:
            worksheet = sheet.worksheet(name)
        except gspread.WorksheetNotFound:
            worksheet = sheet.add_worksheet(
                title=name, rows=1000, cols=max(len(header), 26)
            )
            worksheet.update([list(header)], "A1")
            log.info("created worksheet %r in tenant %s's sheet", name, self.tenant_id)
            return worksheet

        current = worksheet.row_values(1)
        if current != list(header):
            # Headers drifting is how a Looker report silently starts reading
            # the wrong column. Repair rather than append under a wrong header.
            worksheet.update([list(header)], "A1")
            log.warning("repaired header row in worksheet %r", name)
        return worksheet

    def upsert_lead(self, state: LeadState) -> str:
        worksheet = self._worksheet(self.worksheet_name, CRM_COLUMNS)
        lead_id = state.get("lead_id", "")
        row = lead_to_crm_row(state)

        existing = worksheet.col_values(1)
        if lead_id in existing:
            index = existing.index(lead_id) + 1
            retry_once(
                worksheet.update, [row], f"A{index}", _label="sheets_update"
            )
            return f"updated row {index}"
        retry_once(worksheet.append_row, row, _label="sheets_append")
        return "appended"

    def append_suppression(self, entry: dict[str, Any]) -> None:
        worksheet = self._worksheet(self.suppression_worksheet, SUPPRESSION_COLUMNS)
        retry_once(
            worksheet.append_row,
            [str(entry.get(k, "")) for k in SUPPRESSION_COLUMNS],
            _label="sheets_suppression",
        )


# --------------------------------------------------------------------------- #
# Airtable
# --------------------------------------------------------------------------- #

class AirtableCRM(CRMBackend):
    """Airtable free plan, same column contract."""

    def __init__(self, tenant_id: str, table_name: str = "Leads"):
        super().__init__(tenant_id)
        self.table_name = table_name

    def _table(self, name: str):
        from pyairtable import Table

        api_key = env("AIRTABLE_API_KEY")
        base_id = env_for_tenant("AIRTABLE_BASE_ID", self.tenant_id)
        if not api_key or not base_id:
            raise RuntimeError(
                f"Airtable is not configured for tenant '{self.tenant_id}'. Set "
                f"AIRTABLE_API_KEY and AIRTABLE_BASE_ID__{self.tenant_id}."
            )
        return Table(api_key, base_id, name)

    def upsert_lead(self, state: LeadState) -> str:
        table = self._table(self.table_name)
        fields = lead_to_crm_dict(state)
        lead_id = fields["lead_id"]
        matches = retry_once(
            table.all, formula=f"{{lead_id}}='{lead_id}'", _label="airtable_search"
        )
        if matches:
            retry_once(table.update, matches[0]["id"], fields, _label="airtable_update")
            return "updated"
        retry_once(table.create, fields, _label="airtable_create")
        return "created"

    def append_suppression(self, entry: dict[str, Any]) -> None:
        table = self._table("Suppression")
        retry_once(
            table.create,
            {k: str(entry.get(k, "")) for k in SUPPRESSION_COLUMNS},
            _label="airtable_suppression",
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def get_backend(
    tenant_id: str,
    *,
    backend: str = "",
    worksheet_name: str = "Leads",
    suppression_worksheet: str = "Suppression",
    dry_run: bool = False,
) -> CRMBackend:
    """
    Resolve the CRM backend for one tenant.

    A dry run always gets DryRunCRM, and so does any run whose credentials are
    missing -- a batch must not fail at the last node because a service account
    file was not set up yet. The operator sees the CSV path in the log.
    """
    if dry_run:
        return DryRunCRM(tenant_id)

    backend = (backend or env("CRM_BACKEND", "google_sheets")).lower()

    if backend in ("hubspot", "pipedrive"):
        # Imported here rather than at module scope: sheets_crm defines the
        # base class crm_rest imports, so a top-level import would be circular.
        from src.integrations.crm_rest import HubSpotCRM, PipedriveCRM

        if backend == "hubspot" and env("HUBSPOT_API_KEY"):
            return HubSpotCRM(tenant_id, table_name=worksheet_name)
        if backend == "pipedrive" and env("PIPEDRIVE_API_KEY") and (
            env_for_tenant("PIPEDRIVE_DOMAIN", tenant_id) or env("PIPEDRIVE_DOMAIN")
        ):
            return PipedriveCRM(tenant_id, table_name=worksheet_name)
        log.warning(
            "%s is not configured for tenant %s; writing CRM rows to CSV instead",
            backend, tenant_id,
        )
        return DryRunCRM(tenant_id)

    if backend == "airtable":
        if env("AIRTABLE_API_KEY") and env_for_tenant("AIRTABLE_BASE_ID", tenant_id):
            return AirtableCRM(tenant_id, table_name=worksheet_name)
        log.warning(
            "Airtable not configured for tenant %s; writing CRM rows to CSV instead",
            tenant_id,
        )
        return DryRunCRM(tenant_id)

    if env_for_tenant("GOOGLE_SHEETS_SPREADSHEET_ID", tenant_id):
        return GoogleSheetsCRM(
            tenant_id,
            worksheet_name=worksheet_name,
            suppression_worksheet=suppression_worksheet,
        )

    log.warning(
        "no GOOGLE_SHEETS_SPREADSHEET_ID__%s configured; writing CRM rows to CSV instead",
        tenant_id,
    )
    return DryRunCRM(tenant_id)


def append_suppression(tenant_id: str, entry: dict[str, Any]) -> None:
    """
    Module-level helper used by integrations/suppression.py to mirror an entry.

    Kept as a free function so the suppression list does not need to know which
    backend a tenant uses, or construct one on a path where a failure is
    already tolerated.
    """
    get_backend(tenant_id).append_suppression(entry)
