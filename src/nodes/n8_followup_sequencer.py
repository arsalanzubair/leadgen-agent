"""
N8 -- Multi-Touch Follow-Up Sequencer.

In:  sequence_step, reply_category, channel, config/cadences.yaml
Out: sequence_step, next_action, next_touch_due

Email cadence: N touches with defined day-gaps.
LinkedIn cadence: connect -> await acceptance -> DM -> wait -> follow-up DM.

Loops back to N4 for the next touch, or archives after the final step with no
reply.

TWO RULES THAT ARE NOT NEGOTIABLE
---------------------------------
1. An `interested` reply exits the automated sequence IMMEDIATELY and sets
   next_action to a same-day manual follow-up. A scheduled touch is never sent
   to someone who already replied positively -- that is the single most
   damaging thing this system could do.
2. The effective touch ceiling is the SHORTER of the cadence length and the
   region's `max_touches_without_reply`. An EU lead on a 3-touch cadence stops
   after 2.

A LinkedIn step marked `requires: connection_accepted` does not become due
until the operator marks the lead connected in the manual queue -- and is
abandoned after `abandon_if_unmet_after_days` rather than waiting forever.

THE `sequence_step` CONVENTION
------------------------------
`sequence_step` counts COMPLETED touches, and N8 is the only node that changes
it. N4 therefore drafts touch `sequence_step + 1`. Because N8 runs after the
touch it is counting, on entry the value is still the pre-touch count:

    seq=0  -> N4 drafts touch 1 -> sent -> N8 sets seq=1, schedules touch 2
    seq=1  -> N4 drafts touch 2 -> sent -> N8 sets seq=2, schedules touch 3

So inside this node the touch being SCHEDULED is `next_step + 1`, not
`next_step`. Getting that off by one silently skips a cadence step.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.nodes.n6b_linkedin_outreach import is_connection_accepted
from src.reliability import log, node
from src.state import LeadState, ReplyCategory, utcnow


def cadence_channel(channel: str) -> str:
    """Which cadence a lead follows. `both` has its own interleaved cadence."""
    return channel if channel in ("email", "linkedin", "both") else "email"


def step_config(config: TenantConfig, channel: str, step: int) -> dict[str, Any]:
    """The cadence entry for `step` (1-based), or {} when past the end."""
    steps = config.cadence_for(cadence_channel(channel)).get("steps", [])
    for entry in steps:
        if int(entry.get("step", 0)) == step:
            return dict(entry)
    return {}


def is_step_blocked(
    state: LeadState, step_cfg: dict[str, Any]
) -> tuple[bool, str]:
    """
    Is this step waiting on an external precondition?

    Currently only `connection_accepted`, which the operator sets in the
    LinkedIn queue. A blocked step holds rather than fires -- DMing someone who
    never accepted the connection is not possible, so pretending otherwise just
    burns a cadence slot.
    """
    requirement = step_cfg.get("requires")
    if not requirement:
        return False, ""
    if requirement == "connection_accepted":
        if is_connection_accepted(state["tenant_id"], state.get("lead_id", "")):
            return False, ""
        return True, "waiting for the LinkedIn connection to be accepted"
    return True, f"waiting on unmet requirement {requirement!r}"


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n8_followup_sequencer")
def n8_followup_sequencer(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """
    Advance the cadence by one step, or end it.

    Called after every touch, and again on each scheduled run for leads still
    in a sequence.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])
    channel = cadence_channel(state.get("channel", "email"))
    category = state.get("reply_category", ReplyCategory.NO_REPLY.value)
    current_step = int(state.get("sequence_step", 0))
    now = utcnow()

    # ---------------------------------------------------------------- #
    # 1. Replies end or defer the sequence before anything else happens.
    # ---------------------------------------------------------------- #
    rule = config.on_reply_rule(category)

    if category == ReplyCategory.INTERESTED.value:
        log.info(
            "INTERESTED reply tenant=%s lead=%s company=%r - exiting the sequence",
            state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        )
        return {
            "sequence_step": current_step,
            "next_touch_due": None,
            "archived": True,
            "archive_reason": "replied_interested",
            "next_action": rule.get("next_action") or (
                "MANUAL: same-day personal follow-up - reply from the sending "
                "inbox, do not send another sequenced touch"
            ),
        }

    if category == ReplyCategory.NOT_INTERESTED.value:
        return {
            "sequence_step": current_step,
            "next_touch_due": None,
            "archived": True,
            "archive_reason": "replied_not_interested",
            "next_action": rule.get("next_action") or "closed - added to suppression list",
        }

    if category == ReplyCategory.OBJECTION.value:
        return {
            "sequence_step": current_step,
            "next_touch_due": None,
            "archived": True,
            "archive_reason": "replied_objection",
            "needs_manual_review": True,
            "manual_review_reason": f"objection received: {state.get('reply_text', '')[:200]}",
            "next_action": rule.get("next_action") or (
                "MANUAL: review the objection and decide whether to respond personally"
            ),
        }

    if category == ReplyCategory.OUT_OF_OFFICE.value:
        # An OOO is not a reply and must not burn a touch. Push the next one out.
        defer_days = int(rule.get("defer_days", 7))
        due = now + timedelta(days=defer_days)
        log.info(
            "out of office tenant=%s lead=%s; deferring the next touch %d days",
            state.get("tenant_id"), state.get("lead_id"), defer_days,
        )
        return {
            "sequence_step": current_step,      # unchanged: no touch consumed
            "next_touch_due": due,
            "reply_category": ReplyCategory.NO_REPLY.value,   # clear it for next time
            "reply_text": "",
            "next_action": rule.get("next_action") or (
                f"out of office detected - next touch deferred {defer_days} days"
            ),
        }

    # ---------------------------------------------------------------- #
    # 2. No reply: advance the cadence.
    # ---------------------------------------------------------------- #
    ceiling = config.max_touches(channel, state["region"])
    cadence = config.cadence_for(channel)
    next_step = current_step + 1

    if next_step >= ceiling:
        cadence_len = len(cadence.get("steps", []))
        why = (
            f"region cap of {ceiling}" if ceiling < cadence_len
            else f"cadence of {cadence_len} touches"
        )
        log.info(
            "cadence exhausted tenant=%s lead=%s company=%r after %d touch(es) (%s)",
            state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
            current_step, why,
        )
        return {
            "sequence_step": current_step,
            "next_touch_due": None,
            "archived": True,
            "archive_reason": cadence.get("archive_reason", "cadence_exhausted"),
            "next_action": f"sequence complete after {current_step} touch(es) - no reply ({why})",
        }

    upcoming = step_config(config, channel, next_step + 1)
    if not upcoming:
        return {
            "sequence_step": current_step,
            "next_touch_due": None,
            "archived": True,
            "archive_reason": cadence.get("archive_reason", "cadence_exhausted"),
            "next_action": f"sequence complete after {current_step} touch(es) - no reply",
        }

    # -- blocked on an external precondition (LinkedIn acceptance) ---------- #
    blocked, why = is_step_blocked(state, upcoming)
    if blocked:
        last_touch = state.get("last_touch_at") or now
        abandon_after = upcoming.get("abandon_if_unmet_after_days")
        if abandon_after and (now - last_touch) > timedelta(days=int(abandon_after)):
            log.info(
                "abandoning tenant=%s lead=%s: %s for over %s days",
                state.get("tenant_id"), state.get("lead_id"), why, abandon_after,
            )
            return {
                "sequence_step": current_step,
                "next_touch_due": None,
                "archived": True,
                "archive_reason": "connection_not_accepted",
                "next_action": f"abandoned: {why} for over {abandon_after} days",
            }
        recheck = now + timedelta(days=1)
        return {
            "sequence_step": current_step,      # no touch consumed while waiting
            "next_touch_due": recheck,
            "next_action": f"holding: {why}",
        }

    # -- schedule the next touch ------------------------------------------- #
    wait_days = int(upcoming.get("wait_days", 3))
    base = state.get("last_touch_at") or state.get("sent_at") or now
    due = base + timedelta(days=wait_days)
    if due < now:
        due = now

    log.info(
        "advancing cadence tenant=%s lead=%s company=%r step %d -> %d (%s), due %s",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        current_step, next_step, upcoming.get("name", ""), due.isoformat(),
    )

    return {
        "sequence_step": next_step,
        "next_touch_due": due,
        # Reset the per-touch send state so N4/N5/N5.5 re-run cleanly for the
        # next touch -- in particular approval, which must be granted again.
        "approval_status": "pending",
        "send_status": "not_sent",
        "suppression_status": "clear",
        "next_action": (
            f"touch {next_step + 1} ({upcoming.get('name', '')}) due "
            f"{due:%Y-%m-%d} on {upcoming.get('channel', channel)}"
        ),
    }


def route_after_sequencer(state: LeadState) -> str:
    """
    Section 5: N8 loops back to N4 for the next touch, or goes to N9 when the
    cadence ends.

    The loop is only taken when the next touch is ACTUALLY DUE. Looping
    immediately would run the whole cadence inside one invocation and send all
    three touches in the same minute -- the day-gaps in cadences.yaml are the
    entire point. A touch scheduled for the future ends this run at N9; the
    next scheduled run resumes the thread and takes the loop then.
    """
    if state.get("archived") or state.get("needs_manual_review"):
        return "crm"
    if not is_touch_due(state):
        return "crm"
    return "personalization"


def is_touch_due(state: LeadState, *, now=None) -> bool:
    """
    Is this lead's next touch due yet?

    Used by cli/run_batch.py to decide which in-flight leads to resume on a
    scheduled run, rather than re-running the whole book every time.
    """
    due = state.get("next_touch_due")
    if due is None:
        return False
    return due <= (now or utcnow())
