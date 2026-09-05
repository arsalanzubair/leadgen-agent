"""
N6b -- LinkedIn Outreach (semi-automated by design).

In:  cleared LeadState whose channel includes linkedin
Out: send_status = pending_manual_send

Writes the approved, compliance-cleared message into a per-tenant manual-send
queue. A human opens LinkedIn, sends it, and marks it sent through
cli/linkedin_queue.py.

DELIBERATE CONSTRAINT
---------------------
There is no LinkedIn UI automation here and there must never be: no headless
browser, no session cookie replay, no API that acts on a personal account.
This is an account-ban-risk decision, not an unfinished feature. If a future
change looks like it wants to automate the send, extend the queue and the CLI
instead. The only thing this node does is prepare work for a person.

The queue is capped by the tenant's `daily_send_limits.linkedin_manual`, which
exists so the queue stays something a human can actually clear -- a 200-item
backlog is the same as no queue at all.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import SkipLead, log, node
from src.settings import global_dry_run, linkedin_queue_path
from src.state import MAX_LINKEDIN_CONNECTION_NOTE_CHARS, LeadState, SendStatus, utcnow

#: Queue item states. `connected` is set by the operator once the person
#: accepts, which is what unblocks cadence step 2 (the post-acceptance DM).
QUEUE_STATUSES = ("pending", "sent", "connected", "skipped")


# --------------------------------------------------------------------------- #
# Queue storage (per tenant)
# --------------------------------------------------------------------------- #

def load_queue(tenant_id: str) -> dict[str, Any]:
    path = linkedin_queue_path(tenant_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tenant_id": tenant_id, "items": {}}
    if data.get("tenant_id") != tenant_id:
        raise ValueError(
            f"LinkedIn queue at {path} declares tenant_id {data.get('tenant_id')!r} "
            f"but was loaded for {tenant_id!r}"
        )
    data.setdefault("items", {})
    return data


def save_queue(tenant_id: str, data: dict[str, Any]) -> None:
    path = linkedin_queue_path(tenant_id)
    data["tenant_id"] = tenant_id
    data["updated_at"] = utcnow().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def enqueue(tenant_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Add or refresh one queue item, keyed by lead_id + sequence step."""
    data = load_queue(tenant_id)
    key = f"{item['lead_id']}::{item.get('sequence_step', 1)}"
    existing = data["items"].get(key, {})
    if existing.get("status") in ("sent", "connected"):
        # Never resurrect something a human already actioned.
        return existing
    item.setdefault("status", "pending")
    item.setdefault("queued_at", utcnow().isoformat())
    data["items"][key] = item
    save_queue(tenant_id, data)
    return item


def pending_items(tenant_id: str) -> list[dict[str, Any]]:
    items = load_queue(tenant_id)["items"].values()
    return sorted(
        (i for i in items if i.get("status") == "pending"),
        key=lambda i: (-int(i.get("fit_score", 0)), i.get("queued_at", "")),
    )


def all_items(tenant_id: str, status: str = "") -> list[dict[str, Any]]:
    items = list(load_queue(tenant_id)["items"].values())
    if status:
        items = [i for i in items if i.get("status") == status]
    return sorted(items, key=lambda i: i.get("queued_at", ""), reverse=True)


def mark(
    tenant_id: str, lead_id: str, status: str, *, sequence_step: int | None = None
) -> dict[str, Any] | None:
    """
    Mark a queued item. Called by the CLI once the human has actually acted.

    With no `sequence_step` it marks the lead's most recent pending item, which
    is what an operator working through the list means by "mark this one sent".
    """
    if status not in QUEUE_STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {QUEUE_STATUSES}")

    data = load_queue(tenant_id)
    if sequence_step is not None:
        keys = [f"{lead_id}::{sequence_step}"]
    else:
        keys = sorted(
            (k for k in data["items"] if k.startswith(f"{lead_id}::")
             and data["items"][k].get("status") == "pending"),
            key=lambda k: int(k.rsplit("::", 1)[1]),
        )
        if not keys:
            keys = sorted(k for k in data["items"] if k.startswith(f"{lead_id}::"))

    for key in keys:
        if key in data["items"]:
            data["items"][key]["status"] = status
            data["items"][key][f"{status}_at"] = utcnow().isoformat()
            save_queue(tenant_id, data)
            log.info("linkedin queue tenant=%s lead=%s -> %s", tenant_id, lead_id, status)
            return data["items"][key]
    return None


def queued_today(tenant_id: str) -> int:
    today = utcnow().date().isoformat()
    return sum(
        1 for item in load_queue(tenant_id)["items"].values()
        if str(item.get("queued_at", "")).startswith(today)
    )


def is_connection_accepted(tenant_id: str, lead_id: str) -> bool:
    """
    Has the operator marked this lead's connection request accepted?

    N8 needs this: cadence step 2 (the post-acceptance DM) must not become due
    for someone who never accepted the request.
    """
    data = load_queue(tenant_id)
    return any(
        item.get("status") == "connected"
        for key, item in data["items"].items()
        if key.startswith(f"{lead_id}::")
    )


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n6b_linkedin_outreach")
def n6b_linkedin_outreach(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """
    Queue this touch's LinkedIn message for a human to send.

    In a dry run the message is printed and NOT queued -- a dry run must not
    leave real work in an operator's queue.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])

    draft = (state.get("draft_message") or {}).get("linkedin") or {}
    note = draft.get("connection_note", "")
    dm = draft.get("followup_dm", "")
    profile_url = (state.get("linkedin_url") or "").strip()

    if not profile_url or not (note or dm):
        raise SkipLead(
            send_status=SendStatus.FAILED.value,
            needs_manual_review=True,
            manual_review_reason="LinkedIn send attempted with no profile URL or no draft",
            next_action="MANUAL: incomplete LinkedIn draft",
        )

    if state.get("suppression_status") != "clear":
        raise SkipLead(
            send_status=SendStatus.NOT_SENT.value,
            archived=True,
            archive_reason="suppressed",
            next_action="not queued: suppression gate did not clear this lead",
        )

    if len(note) > MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
        # Belt and braces: N4 enforces this, but a human edit at N5 could have
        # reintroduced an over-length note.
        raise SkipLead(
            send_status=SendStatus.FAILED.value,
            needs_manual_review=True,
            manual_review_reason=(
                f"connection note is {len(note)} chars, over the "
                f"{MAX_LINKEDIN_CONNECTION_NOTE_CHARS} character limit"
            ),
            next_action="MANUAL: shorten the connection note",
        )

    step = int(state.get("sequence_step", 0)) + 1
    dry_run = bool(state.get("dry_run")) or global_dry_run()

    if dry_run:
        print("\n" + "=" * 72)
        print("DRY RUN -- this LinkedIn message would be QUEUED for manual send")
        print("=" * 72)
        print(f"Account:  {config.sending_identity.get('linkedin_account_label', '')}")
        print(f"Profile:  {profile_url}")
        print(f"Company:  {state.get('company_name', '')}  (touch {step})")
        print("-" * 72)
        if note:
            print(f"Connection note ({len(note)}/300 chars):\n{note}\n")
        if dm:
            print(f"Follow-up DM:\n{dm}")
        print("=" * 72 + "\n")
        return {
            "send_status": SendStatus.PENDING_MANUAL_SEND.value,
            "last_touch_at": utcnow(),
            "next_action": "dry run: LinkedIn message printed, nothing queued",
        }

    # -- manual-action budget ---------------------------------------------- #
    daily_cap = int(config.daily_send_limits.get("linkedin_manual", 15))
    if queued_today(state["tenant_id"]) >= daily_cap:
        log.warning(
            "linkedin_manual daily cap of %d reached for tenant=%s; holding lead=%s",
            daily_cap, state["tenant_id"], state.get("lead_id"),
        )
        return {
            "send_status": SendStatus.RATE_LIMITED.value,
            "next_action": (
                f"held: {daily_cap} LinkedIn actions already queued today - "
                "queued on the next run"
            ),
        }

    enqueue(state["tenant_id"], {
        "lead_id": state.get("lead_id", ""),
        "sequence_step": step,
        "company_name": state.get("company_name", ""),
        "contact_name": state.get("contact_name", ""),
        "linkedin_url": profile_url,
        "region": state.get("region", ""),
        "niche_id": state.get("niche_id", ""),
        "fit_score": state.get("fit_score", 0),
        "fit_reason": state.get("fit_reason", ""),
        "connection_note": note,
        "followup_dm": dm,
        "language": state.get("language", "en"),
        "account_label": config.sending_identity.get("linkedin_account_label", ""),
        "status": "pending",
    })

    log.info(
        "queued for manual LinkedIn send tenant=%s lead=%s company=%r touch=%d",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"), step,
    )
    return {
        "send_status": SendStatus.PENDING_MANUAL_SEND.value,
        "last_touch_at": utcnow(),
        "next_action": (
            "queued for manual LinkedIn send - run "
            f"`python -m src.cli.linkedin_queue --tenant {state['tenant_id']} list`"
        ),
    }
