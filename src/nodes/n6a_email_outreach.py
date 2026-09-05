"""
N6a -- Email Outreach.

In:  cleared LeadState whose channel includes email
Out: send_status, sent_at

Sends through the configured EmailSender (Gmail SMTP or Brevo). Three
behaviours are functional requirements rather than niceties:

  * A daily send counter PER TENANT PER PROVIDER. Hitting the cap sets
    send_status='rate_limited' and queues the remainder for the next scheduled
    run -- it never fails the batch.
  * A per-region business-hours window, so nothing goes out at 3am local time.
    A touch that becomes due outside the window is deferred to the next open
    window rather than dropped.
  * Dry run prints what would be sent and sends nothing. GLOBAL_DRY_RUN
    overrides everything, including the CLI flag and the tenant config, so an
    auto-approval in a dry run can never become a real send.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.integrations.email_sender import DailyLimitReached
from src.providers import sender_for
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import SkipLead, log, node
from src.settings import global_dry_run
from src.state import LeadState, SendStatus, utcnow

#: Fallback when a compliance profile omits business_hours.
DEFAULT_HOURS: dict[str, Any] = {
    "timezone": "UTC", "start_hour": 9, "end_hour": 17, "weekdays": [0, 1, 2, 3, 4],
}


# --------------------------------------------------------------------------- #
# Business hours
# --------------------------------------------------------------------------- #

def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        # Windows has no system tz database; `tzdata` in requirements.txt
        # supplies it. If even that is missing, UTC is a safe, visible default.
        log.warning("unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


def business_hours_for(config: TenantConfig, region: str) -> dict[str, Any]:
    profile = config.compliance_profile(region)
    hours = dict(DEFAULT_HOURS)
    hours.update(profile.get("business_hours") or {})
    return hours


def is_within_business_hours(
    config: TenantConfig, region: str, when: datetime | None = None
) -> tuple[bool, str]:
    """
    Is `when` inside the region's sending window?

    The window is the compliance profile's business hours, narrowed by
    cadences.yaml `sending_windows` (skip the first and last hour of the day --
    inbox noise is worst then). Returns (ok, reason_if_not).
    """
    hours = business_hours_for(config, region)
    windows = config.sending_windows
    zone = _zone(str(hours.get("timezone", "UTC")))
    local = (when or utcnow()).astimezone(zone)

    weekdays = [int(d) for d in hours.get("weekdays", DEFAULT_HOURS["weekdays"])]
    if local.weekday() not in weekdays:
        return False, f"{local:%A} is not a working day in {region}"

    start = int(hours.get("start_hour", 9))
    end = int(hours.get("end_hour", 17))
    if windows.get("avoid_first_hour", True):
        start += 1
    if windows.get("avoid_last_hour", True):
        end -= 1
    if start >= end:                      # a config so narrow it has no window
        start, end = int(hours.get("start_hour", 9)), int(hours.get("end_hour", 17))

    if not (start <= local.hour < end):
        return False, (
            f"{local:%H:%M} {zone.key} is outside the {start:02d}:00-{end:02d}:00 "
            f"sending window for {region}"
        )

    if local.date().isoformat() in (windows.get("skip_dates") or []):
        return False, f"{local.date()} is in the cadence skip_dates list"

    return True, ""


def next_open_window(
    config: TenantConfig, region: str, after: datetime | None = None
) -> datetime:
    """
    The next moment inside the region's sending window, in UTC.

    Steps hour by hour rather than solving it analytically -- the working week
    differs per region (the Middle East profile runs Sunday to Thursday) and an
    hour loop over at most a week is both obviously correct and fast enough.
    """
    when = (after or utcnow()).replace(minute=0, second=0, microsecond=0)
    for _ in range(24 * 8):
        when += timedelta(hours=1)
        ok, _reason = is_within_business_hours(config, region, when)
        if ok:
            return when
    return (after or utcnow()) + timedelta(days=1)


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n6a_email_outreach")
def n6a_email_outreach(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """
    Send this touch's email.

    Ordering matters: the send-time safety checks run in the order a mistake
    would be most costly -- global kill switch, then approval and clearance,
    then the business-hours window, then the daily budget, then the send.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])

    draft = (state.get("draft_message") or {}).get("email") or {}
    subject = draft.get("subject", "")
    body = draft.get("body", "")
    to = (state.get("contact_email") or "").strip()

    if not to or not subject or not body:
        raise SkipLead(
            send_status=SendStatus.FAILED.value,
            needs_manual_review=True,
            manual_review_reason="email send attempted with no address or no draft",
            next_action="MANUAL: incomplete email draft",
        )

    # -- last-line safety: nothing sends unless it was cleared HERE --------- #
    if state.get("suppression_status") != "clear":
        raise SkipLead(
            send_status=SendStatus.NOT_SENT.value,
            archived=True,
            archive_reason="suppressed",
            next_action="not sent: suppression gate did not clear this lead",
        )

    dry_run = bool(state.get("dry_run")) or global_dry_run()

    # -- business hours ----------------------------------------------------- #
    in_hours, reason = is_within_business_hours(config, state["region"])
    if not in_hours and not dry_run:
        due = next_open_window(config, state["region"])
        log.info(
            "deferring send tenant=%s lead=%s: %s (next window %s)",
            state.get("tenant_id"), state.get("lead_id"), reason, due.isoformat(),
        )
        return {
            "send_status": SendStatus.RATE_LIMITED.value,
            "next_touch_due": due,
            "next_action": f"deferred: {reason}",
        }

    identity = config.sending_identity
    # `sending_identity.provider` still works -- the resolver reads it as a
    # legacy field when the tenant has no `providers:` block -- but the
    # workspace's own choice comes first now.
    sender = sender_for(config, tenant_id=state["tenant_id"], dry_run=dry_run)

    # -- daily budget ------------------------------------------------------- #
    try:
        sender.check_budget()
    except DailyLimitReached as exc:
        log.warning(
            "daily send limit reached tenant=%s provider=%s; queueing lead=%s "
            "for the next run", state["tenant_id"], sender.provider, state.get("lead_id"),
        )
        return {
            "send_status": SendStatus.RATE_LIMITED.value,
            "next_touch_due": next_open_window(config, state["region"]),
            "next_action": f"queued for the next run: {exc}",
        }

    # -- send --------------------------------------------------------------- #
    result = sender.send(
        to=to,
        to_name=state.get("contact_name", ""),
        subject=subject,
        body=body,
        from_name=identity.get("from_name", ""),
        from_email=identity.get("from_email", ""),
        reply_to=identity.get("reply_to", ""),
    )

    if not result.ok:
        return {
            "send_status": SendStatus.FAILED.value,
            "needs_manual_review": True,
            "manual_review_reason": f"email send failed: {result.detail}",
            "next_action": "MANUAL: investigate the send failure",
        }

    now = utcnow()
    # A `both` lead still owes a LinkedIn touch, but N6b runs next and sets
    # pending_manual_send itself -- N6a reports only on the email it sent.
    return {
        "send_status": SendStatus.SENT.value,
        "sent_at": now,
        "last_touch_at": now,
        "next_action": (
            "dry run: email printed, nothing sent"
            if dry_run else
            f"email sent via {result.provider}; watching for a reply"
        ),
    }
