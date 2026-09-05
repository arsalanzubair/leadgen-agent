"""
N9 -- CRM Logging and Analytics.

In:  terminal LeadState
Out: an upserted row in the tenant's CRM (Google Sheet or Airtable)

Writes one row per lead using state.CRM_COLUMNS -- clean headers, enum values
as plain strings, ISO-8601 timestamps -- so a Looker Studio report can be
pointed straight at the sheet with no transformation layer.

This node is the graph's single exit: every terminal path (archived, rejected,
suppressed, unreachable, replied, cadence exhausted, needs manual review)
routes through here first. A lead that vanished without a CRM row would be a
lead nobody can account for, which is worse than one with an unflattering
status.

A CRM write failure never fails the lead. The row is the record of work
already done; losing the record is bad, but re-sending an email because a
Sheets write failed would be worse.
"""

from __future__ import annotations

from typing import Any

from src.providers import crm_for
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.state import LeadState, lead_to_crm_dict


def outcome_of(state: LeadState) -> str:
    """
    A single human-readable outcome for the lead, for the operator's summary
    line and for grouping in Looker. Derived rather than stored, so it can
    never drift out of sync with the fields it summarises.
    """
    if state.get("needs_manual_review"):
        return "needs_manual_review"
    if state.get("reply_category") == "interested":
        return "interested_manual_handoff"
    if state.get("unreachable"):
        return "unreachable"
    if state.get("suppression_status") == "blocked_optout":
        return "suppressed"
    if state.get("suppression_status") == "blocked_compliance":
        return "blocked_compliance"
    if state.get("archived"):
        return state.get("archive_reason") or "archived"
    if state.get("send_status") == "rate_limited":
        return "queued_for_next_run"
    if state.get("send_status") == "pending_manual_send":
        return "awaiting_manual_linkedin_send"

    # N8 resets approval_status and send_status when it schedules the next
    # touch, so a lead mid-cadence looks untouched if you only read those.
    # last_touch_at is the field that says whether anything has gone out.
    if state.get("next_touch_due") and state.get("last_touch_at"):
        return f"in_sequence_touch_{int(state.get('sequence_step', 0)) + 1}_scheduled"
    if state.get("send_status") == "sent":
        return "sent_awaiting_reply"
    if state.get("approval_status") == "pending" and not state.get("last_touch_at"):
        return "awaiting_approval"
    return state.get("send_status", "in_progress")


@node("n9_crm_analytics")
def n9_crm_analytics(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """Write this lead's row to the tenant's CRM."""
    config = tenant_config or load_tenant_config(state["tenant_id"])

    # The worksheet names still come from the `crm:` block; only who writes to
    # them is now the workspace's choice. `crm.backend` is read as a legacy
    # field, so a config written before the `providers:` block behaves the same.
    backend = crm_for(
        config, tenant_id=state["tenant_id"], dry_run=bool(state.get("dry_run"))
    )

    outcome = outcome_of(state)
    updates: dict[str, Any] = {}

    try:
        detail = backend.upsert_lead(state)
        log.info(
            "crm tenant=%s lead=%s company=%r outcome=%s (%s)",
            state.get("tenant_id"), state.get("lead_id"),
            state.get("company_name"), outcome, detail,
        )
    except Exception as exc:  # noqa: BLE001
        # Deliberately NOT re-raised: see the module docstring. The work is
        # already done; only the record failed.
        log.error(
            "CRM write failed tenant=%s lead=%s (%s); the lead's outcome is "
            "unchanged and the row can be replayed",
            state.get("tenant_id"), state.get("lead_id"), exc,
        )
        updates["errors"] = [
            *(state.get("errors") or []),
            {"node": "n9_crm_analytics", "error": str(exc), "at": ""},
        ]

    if not state.get("next_action"):
        updates["next_action"] = outcome
    return updates


def summarise(states: list[LeadState]) -> dict[str, int]:
    """Outcome counts for a batch, for the run summary the operator reads."""
    counts: dict[str, int] = {}
    for state in states:
        key = outcome_of(state)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def crm_preview(state: LeadState) -> dict[str, str]:
    """The exact row that would be written. Used by the dry-run summary."""
    return lead_to_crm_dict(state)
