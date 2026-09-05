"""
crm_adapters.py -- where the outcome gets written down.

Like the email capabilities, this one already had its interface
(`CRMBackend`) and its factory (`get_backend`) before the capability layer
existed, and `GoogleSheetsCRM`, `AirtableCRM` and `DryRunCRM` all satisfy the
`CRMProvider` Protocol structurally. Only the choice moves.

Note on the registry: Airtable is listed as built, not "coming soon", because
`AirtableCRM` exists and works. The same is true of Brevo for sending. Worth
saying out loud so nobody re-implements either of them believing they are
placeholders.
"""

from __future__ import annotations

from src.integrations import sheets_crm as _crm_module
from src.integrations.sheets_crm import CRMBackend, DryRunCRM
from src.providers.base import ProviderContext
from src.providers.registry import ProviderSpec


def build(spec: ProviderSpec, ctx: ProviderContext) -> CRMBackend:
    """
    Registry factory.

    `csv` is the local-file backend: a real, selectable choice for somebody who
    does not want their lead list in a cloud service, and also what an
    unconfigured workspace falls back to. `get_backend` already degrades to it
    when credentials are missing, so a batch never fails at its last node
    because a service account was not set up yet -- the operator gets the CSV
    path in the log instead.
    """
    options = ctx.options
    worksheet = str(options.get("worksheet_name", "Leads") or "Leads")
    suppression = str(
        options.get("suppression_worksheet_name", "Suppression") or "Suppression"
    )

    if spec.id == "csv":
        return DryRunCRM(ctx.tenant_id)

    # Through the module: the end-to-end test patches
    # `src.integrations.sheets_crm.get_backend` to prove one CRM failure does
    # not lose the other 39 leads, and a bound name would bypass that.
    return _crm_module.get_backend(
        ctx.tenant_id,
        backend=spec.id,
        worksheet_name=worksheet,
        suppression_worksheet=suppression,
        dry_run=ctx.dry_run,
    )
