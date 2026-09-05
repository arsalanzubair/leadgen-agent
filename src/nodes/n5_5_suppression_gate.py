"""
N5.5 -- Suppression and Compliance Gate.

In:  contact_email, linkedin_url, region, draft_message
Out: suppression_status (clear | blocked_optout | blocked_compliance)

Runs before EVERY send attempt, on EVERY channel, on EVERY run -- including
every subsequent touch in a cadence. No exceptions, and no caching of a
previous `clear` result: the list is reloaded from disk on every call, because
the whole point is that something added to it an hour ago stops the next touch.

    1. Tenant suppression list, exact match on email and on LinkedIn URL,
       plus the tenant config's own blocked_domains / blocked_emails and the
       process-level BLOCKED_DOMAINS rail
       -> blocked_optout
    2. Region compliance profile checks: required opt-out line present in the
       draft, sender identification present, physical address where required,
       role-based address where required, legal-basis line where required, and
       the region's touch ceiling
       -> blocked_compliance
    3. Otherwise -> clear

This node also owns the auto-suppress rule (`auto_suppress_from_reply`): a
reply classified not_interested, or containing unsubscribe / opt-out language,
or raising an explicit compliance objection, is added to the suppression list
AUTOMATICALLY. N7 calls it the moment a reply is classified, so suppression
never depends on the operator remembering.
"""

from __future__ import annotations

from typing import Any

from src.integrations import suppression
from src.integrations.scraping import is_role_based
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.settings import blocked_domains as global_blocked_domains
from src.state import (
    AUTO_SUPPRESS_CATEGORIES,
    LeadState,
    SuppressionStatus,
    wants_email,
    wants_linkedin,
)


# --------------------------------------------------------------------------- #
# Compliance checks
# --------------------------------------------------------------------------- #

def _draft_text(state: LeadState) -> str:
    """Everything that will actually be sent, as one blob to search."""
    draft = state.get("draft_message") or {}
    email = draft.get("email") or {}
    linkedin = draft.get("linkedin") or {}
    return " ".join(str(x) for x in (
        email.get("subject", ""), email.get("body", ""),
        linkedin.get("connection_note", ""), linkedin.get("followup_dm", ""),
    ) if x)


def _contains_any(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(str(p).lower() in lowered for p in phrases if str(p).strip())


def check_compliance(
    state: LeadState, config: TenantConfig
) -> tuple[bool, list[str]]:
    """
    Evaluate the region's machine-checkable rules against this draft.

    Returns (passed, failures). Every rule here maps to a field in
    config/compliance_profiles.yaml -- if a rule cannot be checked by a
    function, it belongs in that file's operator notes, not in this code path.
    """
    profile = config.compliance_profile(state["region"])
    identity = config.sending_identity
    failures: list[str] = []

    email_body = ((state.get("draft_message") or {}).get("email") or {}).get("body", "")
    all_text = _draft_text(state)
    sending_email = wants_email(state.get("channel", ""))

    # -- opt-out line (email only: LinkedIn has its own platform-level opt-out)
    if profile.get("requires_optout_line") and sending_email:
        phrases = list(profile.get("optout_phrases") or [])
        if not _contains_any(email_body, phrases):
            failures.append(
                f"{state['region']} requires an opt-out line and the email body "
                f"contains none of {phrases[:4]}..."
            )

    # -- sender identification (email only: a LinkedIn message carries the
    #    sender's own profile, which identifies them far better than a name
    #    squeezed into a 300-character connection note)
    if profile.get("requires_sender_identification") and sending_email:
        identifiers = [
            identity.get("company_name", ""),
            identity.get("from_name", ""),
            *(profile.get("identification_phrases") or []),
        ]
        if not _contains_any(all_text, [i for i in identifiers if i]):
            failures.append(
                f"{state['region']} requires the sender to be identified; neither "
                f"{identity.get('from_name')!r} nor "
                f"{identity.get('company_name')!r} appears in the draft"
            )

    # -- physical postal address (CAN-SPAM, CASL)
    if profile.get("requires_physical_address") and sending_email:
        address = str(identity.get("physical_address", "")).strip()
        if not address:
            failures.append(
                f"{state['region']} requires a physical postal address but the "
                "tenant's sending_identity.physical_address is empty"
            )
        elif address.lower() not in email_body.lower():
            failures.append(
                f"{state['region']} requires the physical postal address in the "
                "email body and it is missing"
            )

    # -- role-based address requirement (EU, ME)
    address_email = state.get("contact_email", "")
    if sending_email and address_email:
        requires_role = profile.get("require_role_based_address")
        allows_personal = profile.get("allows_personal_address", True)
        if (requires_role or not allows_personal) and not is_role_based(address_email):
            failures.append(
                f"{state['region']} accepts role-based addresses only; "
                f"{address_email} is a personal address"
            )

    # -- legal basis statement (EU)
    if profile.get("requires_legal_basis_line") and sending_email:
        phrases = list(profile.get("legal_basis_phrases") or [])
        if phrases and not _contains_any(email_body, phrases):
            failures.append(
                f"{state['region']} requires a legal-basis statement and the "
                f"email body contains none of {phrases[:3]}..."
            )

    # -- touch ceiling
    channel = state.get("channel", "email")
    try:
        ceiling = config.max_touches(channel, state["region"])
    except Exception:  # noqa: BLE001 -- an unknown cadence is not a send blocker
        ceiling = None
    if ceiling is not None:
        next_touch = int(state.get("sequence_step", 0)) + 1
        if next_touch > ceiling:
            failures.append(
                f"touch {next_touch} exceeds the ceiling of {ceiling} for "
                f"{channel} in {state['region']}"
            )

    return (not failures), failures


# --------------------------------------------------------------------------- #
# Auto-suppression from a reply (Section 4, N5.5 rule 3)
# --------------------------------------------------------------------------- #

def should_auto_suppress(reply_category: str, reply_text: str) -> tuple[bool, str]:
    """
    Does this reply oblige us to suppress the contact?

    Three independent triggers, any one of which is sufficient:
      * the classifier said `not_interested`;
      * the text contains explicit opt-out language in ANY supported language,
        whatever the classifier decided;
      * the text raises a compliance or data-protection objection.

    The second trigger is why classification alone is not trusted here: a
    misclassified "please remove me" would otherwise keep receiving touches.
    """
    if suppression.contains_compliance_objection(reply_text):
        return True, "compliance objection raised in reply"
    if suppression.contains_opt_out(reply_text):
        return True, "explicit opt-out language in reply"
    if (reply_category or "") in AUTO_SUPPRESS_CATEGORIES:
        return True, f"reply classified {reply_category}"
    return False, ""


def auto_suppress_from_reply(state: LeadState) -> dict[str, Any]:
    """
    Add this lead's contact to the suppression list if their reply requires it.

    Called by N7 as soon as a reply is classified, NOT by the operator and NOT
    only at send time -- an opted-out contact must be on the list before the
    next scheduled touch is even considered.
    """
    reply_text = state.get("reply_text", "") or ""
    category = state.get("reply_category", "") or ""
    should, reason = should_auto_suppress(category, reply_text)
    if not should:
        return {}

    store = suppression.for_tenant(state["tenant_id"])
    store.add(
        email=state.get("contact_email", ""),
        linkedin_url=state.get("linkedin_url", ""),
        reason=reason,
        source="reply",
        lead_id=state.get("lead_id", ""),
    )
    log.info(
        "auto-suppressed tenant=%s lead=%s company=%r reason=%s",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"), reason,
    )
    return {
        "suppression_status": SuppressionStatus.BLOCKED_OPTOUT.value,
        "archived": True,
        "archive_reason": "suppressed",
        "next_action": f"suppressed automatically: {reason}",
    }


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n5_5_suppression_gate")
def n5_5_suppression_gate(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """
    Gate one lead before a send. Never cached, never skipped.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])
    email = (state.get("contact_email") or "").strip()
    linkedin = (state.get("linkedin_url") or "").strip()

    # -- 1. suppression -------------------------------------------------- #
    store = suppression.for_tenant(state["tenant_id"])
    store.reload()          # re-read from disk: a `clear` from an hour ago is stale
    blocked, reason = store.is_suppressed(email=email, linkedin_url=linkedin)

    if not blocked and email:
        domain = suppression.domain_of(email)
        tenant_blocked = config.blocked_domains
        process_blocked = global_blocked_domains()
        if email.lower() in config.blocked_emails:
            blocked, reason = True, "address is in the tenant's blocked_emails"
        elif domain in tenant_blocked:
            blocked, reason = True, f"domain {domain} is in the tenant's blocked_domains"
        elif domain in process_blocked:
            blocked, reason = True, f"domain {domain} is in the BLOCKED_DOMAINS rail"

    if blocked:
        log.info(
            "blocked_optout tenant=%s lead=%s company=%r reason=%s",
            state.get("tenant_id"), state.get("lead_id"),
            state.get("company_name"), reason,
        )
        return {
            "suppression_status": SuppressionStatus.BLOCKED_OPTOUT.value,
            "archived": True,
            "archive_reason": "suppressed",
            "next_action": f"blocked: {reason}",
        }

    # -- 2. compliance ---------------------------------------------------- #
    passed, failures = check_compliance(state, config)
    if not passed:
        log.warning(
            "blocked_compliance tenant=%s lead=%s company=%r region=%s: %s",
            state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
            state.get("region"), "; ".join(failures),
        )
        return {
            "suppression_status": SuppressionStatus.BLOCKED_COMPLIANCE.value,
            "archived": True,
            "archive_reason": "blocked_compliance",
            "needs_manual_review": True,
            "manual_review_reason": "compliance check failed: " + "; ".join(failures),
            "next_action": "MANUAL: fix the draft or the tenant config - " + failures[0],
        }

    # -- 3. clear --------------------------------------------------------- #
    log.info(
        "cleared tenant=%s lead=%s company=%r channel=%s touch=%d",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        state.get("channel"), int(state.get("sequence_step", 0)) + 1,
    )
    return {
        "suppression_status": SuppressionStatus.CLEAR.value,
        "next_action": f"cleared for send on {state.get('channel')}",
    }


def route_after_suppression(state: LeadState) -> str:
    """
    Section 5: the conditional edge after N5.5 routes to N6a / N6b per channel,
    or to archive. A `both` lead goes to email first; N6a hands off to N6b.
    """
    if state.get("suppression_status") != SuppressionStatus.CLEAR.value:
        return "archive"
    channel = state.get("channel", "")
    if wants_email(channel):
        return "email_outreach"
    if wants_linkedin(channel):
        return "linkedin_outreach"
    return "archive"
