"""
N5 -- Human Approval.

In:  draft_message
Out: approval_status (approved | edited | rejected)

Uses the LangGraph `interrupt()` / `Command(resume=...)` pattern against the
per-tenant SqliteSaver, so the queue survives the process exiting and stays
resumable days later. Reviewed through cli/approve.py with a / e / r, grouped
by channel.

WHY interrupt() AND NOT A QUEUE TABLE
-------------------------------------
The lead's entire graph state -- every signal, the fit reason, the cadence
position -- is already checkpointed. Interrupting means the resumed run
continues from exactly that state with no reconstruction step, which is what
makes "approve on Thursday something drafted on Monday" work correctly rather
than approximately.

Dry runs auto-approve (tenant `runtime.auto_approve_in_dry_run`) so the full
graph can be exercised end-to-end without a human at the terminal. That flag
must never be true for a live run, and N6a/N6b independently refuse to send
when GLOBAL_DRY_RUN is set, so an auto-approval cannot become a real send.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.state import APPROVED_STATUSES, LeadState

#: What cli/approve.py renders. Kept here so the payload is defined next to the
#: node that raises it rather than in the CLI that happens to display it.
APPROVAL_ACTIONS = {
    "a": "approve",
    "e": "edit",
    "r": "reject",
}


def build_review_payload(state: LeadState) -> dict[str, Any]:
    """The information an operator needs to decide, and nothing else."""
    draft = state.get("draft_message") or {}
    return {
        "lead_id": state.get("lead_id", ""),
        "tenant_id": state.get("tenant_id", ""),
        "company_name": state.get("company_name", ""),
        "contact_name": state.get("contact_name", ""),
        "contact_email": state.get("contact_email", ""),
        "linkedin_url": state.get("linkedin_url", ""),
        "region": state.get("region", ""),
        "language": state.get("language", ""),
        "niche_id": state.get("niche_id", ""),
        "channel": state.get("channel", ""),
        "fit_score": state.get("fit_score", 0),
        "fit_reason": state.get("fit_reason", ""),
        "signals": list(state.get("signals") or []),
        "sequence_step": int(state.get("sequence_step", 0)) + 1,
        "signal_referenced": draft.get("signal_referenced", ""),
        "translated": bool(draft.get("translated")),
        "email": draft.get("email") or {},
        "linkedin": draft.get("linkedin") or {},
        "actions": APPROVAL_ACTIONS,
    }


def apply_decision(state: LeadState, decision: Any) -> dict[str, Any]:
    """
    Turn an operator's resume value into a state update.

    Accepts either a bare string ("a" / "approve") or a dict carrying edits:

        {"action": "edit",
         "email": {"subject": "...", "body": "..."},
         "linkedin": {"connection_note": "..."}}

    An edit that changes nothing is recorded as `edited` anyway -- the operator
    looked at it and re-saved, and conflating that with an untouched approval
    loses information the CRM should keep.
    """
    if isinstance(decision, str):
        decision = {"action": decision}
    if not isinstance(decision, dict):
        raise ValueError(f"approval decision must be a string or dict, got {decision!r}")

    action = str(decision.get("action", "")).strip().lower()
    action = APPROVAL_ACTIONS.get(action, action)

    if action not in ("approve", "edit", "reject"):
        raise ValueError(
            f"unknown approval action {action!r}; expected one of "
            f"{sorted(set(APPROVAL_ACTIONS.values()))}"
        )

    if action == "reject":
        return {
            "approval_status": "rejected",
            "archived": True,
            "archive_reason": "rejected",
            "next_action": "rejected at review - not sent",
        }

    updates: dict[str, Any] = {"approval_status": "approved"}

    if action == "edit":
        draft = dict(state.get("draft_message") or {})
        if "email" in decision:
            email = dict(draft.get("email") or {})
            email.update({k: v for k, v in (decision["email"] or {}).items() if v is not None})
            draft["email"] = email
        if "linkedin" in decision:
            linkedin = dict(draft.get("linkedin") or {})
            linkedin.update(
                {k: v for k, v in (decision["linkedin"] or {}).items() if v is not None}
            )
            draft["linkedin"] = linkedin
        draft["edited_by_human"] = True
        updates["draft_message"] = draft
        updates["approval_status"] = "edited"

    updates["next_action"] = f"{updates['approval_status']} at review - ready for send checks"
    return updates


@node("n5_human_approval")
def n5_human_approval(state: LeadState, *, tenant_config: TenantConfig | None = None) -> dict:
    """
    Pause the lead's graph thread until a human decides.

    `interrupt()` raises through the LangGraph runtime, checkpoints the state,
    and surfaces the payload to whatever is driving the graph. Resuming with
    `Command(resume=<decision>)` re-runs this node from the top, and the second
    time through `interrupt()` returns the decision instead of pausing.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])

    # A lead that already carries a decision (a resumed thread, or a tenant that
    # has switched approval off) does not pause again.
    existing = state.get("approval_status")
    if existing in APPROVED_STATUSES or existing == "rejected":
        return {}

    if not config.requires_approval:
        log.warning(
            "tenant=%s has require_approval_before_send disabled; auto-approving %s",
            state.get("tenant_id"), state.get("lead_id"),
        )
        return apply_decision(state, "approve")

    if state.get("dry_run") and config.runtime.get("auto_approve_in_dry_run", True):
        log.info(
            "dry run auto-approval tenant=%s lead=%s company=%r",
            state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        )
        update = apply_decision(state, "approve")
        update["next_action"] = "auto-approved (dry run) - no send will occur"
        return update

    decision = interrupt(build_review_payload(state))
    log.info(
        "approval decision tenant=%s lead=%s decision=%r",
        state.get("tenant_id"), state.get("lead_id"), decision,
    )
    return apply_decision(state, decision)


def route_after_approval(state: LeadState) -> str:
    """
    Section 5: the conditional edge after N5 branches on approval_status.
    Anything not explicitly approved or edited leaves the automated path.
    """
    if state.get("needs_manual_review"):
        return "manual_review"
    if state.get("approval_status") in APPROVED_STATUSES:
        return "suppression_gate"
    return "archive"
