"""
N3.5 -- Channel Selection.

In:  niche_id, contact_email, linkedin_url, tenant channel preference
Out: channel ('email' | 'linkedin' | 'both')

RULE-BASED. No LLM call, by design -- this decision is fully determined by two
facts (what the niche prefers, what contact data we actually have) and asking a
model would add latency, cost and non-determinism to a lookup table.

    niche default:  the niche's own `channel_default` field

    overridden by actual data availability:
                    only email      -> email
                    only linkedin   -> linkedin
                    both available  -> tenant preference, default 'both'
                    neither         -> unreachable (N2 already caught this)

Two further constraints narrow the result, in this order:
  * the tenant's `channels.enabled` -- a tenant that has not enabled LinkedIn
    never gets a LinkedIn touch even for a LinkedIn-first niche;
  * the region's compliance profile -- a region that forbids personal email
    addresses will not route an email-only personal address to email.
"""

from __future__ import annotations

from typing import Any

from src.integrations.scraping import is_role_based
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.state import Channel, LeadState

#: Used when a niche declares no `channel_default` of its own. Email, because
#: it is the channel that can actually be automated -- defaulting to LinkedIn
#: would silently route a whole niche into a queue somebody has to work by hand.
FALLBACK_DEFAULT = Channel.EMAIL.value

#: Prefixes the id-based convention used to recognise, kept ONLY to migrate a
#: config written before `channel_default` existed. Anything relying on this
#: gets a warning naming the niche, so it is fixed rather than inherited.
LEGACY_ID_PREFIXES: tuple[tuple[str, str], ...] = (
    ("local_", Channel.EMAIL.value),
    ("b2b_", Channel.LINKEDIN.value),
    ("saas_", Channel.LINKEDIN.value),
)


def niche_default_channel(niche_id: str, niche: dict[str, Any] | None = None) -> str:
    """
    The channel this niche prefers when a lead is reachable both ways.

    Read from the niche's own `channel_default` field.

    This used to be inferred from the id: anything starting `local_` was
    email-first, anything starting `b2b_` or `saas_` was LinkedIn-first. That
    convention breaks the moment a niche is named anything else, which is
    exactly what happens once niches are created through a UI rather than
    hand-written -- an audience called `chicago_law_firms` would have been
    treated as a company audience by accident, and a `local_` prefix on a
    software niche would have quietly sent to the wrong channel. Neither
    failure announces itself; the leads just go out the wrong way.

    The id prefixes are still honoured as a fallback so an older config keeps
    working, but each use logs a warning naming the niche.
    """
    if niche is not None:
        declared = str(niche.get("channel_default") or "").strip().lower()
        if declared in (Channel.EMAIL.value, Channel.LINKEDIN.value, Channel.BOTH.value):
            return declared

    lowered = (niche_id or "").lower()
    for prefix, channel in LEGACY_ID_PREFIXES:
        if lowered.startswith(prefix):
            log.warning(
                "niche %r has no channel_default; falling back to %r from its id "
                "prefix. Add `channel_default:` to this niche -- guessing from the "
                "id breaks for any name the convention did not anticipate.",
                niche_id, channel,
            )
            return channel
    return FALLBACK_DEFAULT


def select_channel(
    *,
    niche_id: str,
    niche: dict[str, Any] | None = None,
    has_email: bool,
    has_linkedin: bool,
    preference_when_both: str = Channel.BOTH.value,
    enabled_channels: tuple[str, ...] | list[str] = ("email", "linkedin"),
    email_allowed: bool = True,
) -> str:
    """
    The whole rule, as a pure function of its inputs. Kept separate from the
    node so the truth table can be tested exhaustively without constructing
    graph state.

    `email_allowed` is the compliance veto: False means this region will not
    accept the address we have (for example a personal address in the EU).
    Returns "" when no channel is possible.
    """
    enabled = {c.lower() for c in enabled_channels}
    email_ok = has_email and email_allowed and "email" in enabled
    linkedin_ok = has_linkedin and "linkedin" in enabled

    if email_ok and linkedin_ok:
        preference = (preference_when_both or Channel.BOTH.value).lower()
        if preference == Channel.EMAIL.value:
            return Channel.EMAIL.value
        if preference == Channel.LINKEDIN.value:
            return Channel.LINKEDIN.value
        if preference == "niche_default":
            return niche_default_channel(niche_id, niche)
        return Channel.BOTH.value
    if email_ok:
        return Channel.EMAIL.value
    if linkedin_ok:
        return Channel.LINKEDIN.value
    return ""


@node("n3_5_channel_selection")
def n3_5_channel_selection(state: LeadState, *, tenant_config: TenantConfig | None = None) -> dict:
    """
    Resolve the outreach channel for one lead.

    A lead that ends up with no usable channel is archived here rather than
    carried forward to fail silently at send time.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])

    email = (state.get("contact_email") or "").strip()
    linkedin = (state.get("linkedin_url") or "").strip()

    # Compliance veto: some regions accept only role-based addresses.
    email_allowed = True
    veto_reason = ""
    if email:
        profile = config.compliance_profile(state["region"])
        requires_role = bool(profile.get("require_role_based_address"))
        allows_personal = profile.get("allows_personal_address", True)
        if (requires_role or not allows_personal) and not is_role_based(email):
            email_allowed = False
            veto_reason = (
                f"{state['region']} compliance profile accepts role-based "
                f"addresses only; {email} is personal"
            )

    niche = config.niche(state["niche_id"]) if state.get("niche_id") else None

    channel = select_channel(
        niche_id=state.get("niche_id", ""),
        niche=niche,
        has_email=bool(email),
        has_linkedin=bool(linkedin),
        preference_when_both=config.preference_when_both,
        enabled_channels=config.enabled_channels,
        email_allowed=email_allowed,
    )

    if veto_reason:
        log.info(
            "channel veto tenant=%s lead=%s: %s",
            state.get("tenant_id"), state.get("lead_id"), veto_reason,
        )

    if not channel:
        reason = veto_reason or "no usable contact route for any enabled channel"
        log.info(
            "no channel tenant=%s lead=%s company=%r: %s",
            state.get("tenant_id"), state.get("lead_id"),
            state.get("company_name"), reason,
        )
        return {
            "archived": True,
            "archive_reason": "no_channel",
            "next_action": f"archived: {reason}",
        }

    updates: dict[str, Any] = {"channel": channel}
    log.info(
        "channel tenant=%s lead=%s company=%r -> %s (niche default %s, "
        "email=%s linkedin=%s)",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        channel, niche_default_channel(state.get("niche_id", ""), niche),
        bool(email), bool(linkedin),
    )
    return updates
