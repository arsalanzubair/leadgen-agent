"""
state.py -- the single data contract for the entire lead-generation graph.

Every node in `src/nodes/` reads from and writes to a `LeadState`. Nothing else
in this system is allowed to invent its own lead representation: if a node needs
to persist a new piece of information, it is added here first.

DESIGN NOTE -- why TypedDict and not a Pydantic model
-----------------------------------------------------
LangGraph merges each node's return value into the graph state as a plain dict
update, and the SqliteSaver checkpointer serialises that state on every
super-step. A `TypedDict` is therefore the cheapest correct choice:

  * nodes can return partial updates (`{"fit_score": 82}`) with no ceremony,
  * state stays trivially JSON/msgpack-serialisable, which is what makes the
    N5 human-approval interrupt resumable across days and process restarts,
  * no per-superstep model construction/validation cost on every lead.

The tradeoff is that a TypedDict gives us *no* runtime validation. We buy that
back explicitly with `validate_lead_state()` (called at the node boundaries that
matter -- N0 output, pre-send in N5.5) instead of paying for it everywhere.
Pydantic models are still used for LLM structured output inside individual nodes
(N3 qualification, N4 personalisation, N7 classification); they are *parsing*
tools there, not the state contract.

ENUM NOTE
---------
Each enum is declared twice on purpose:
  * a `str, Enum` class for programmatic use in node code (`Channel.EMAIL`),
  * a `Literal[...]` alias used in the TypedDict annotations.
The state stores the *plain string value*, never the Enum instance, so that
checkpoints, Google Sheets rows and JSON fixtures all round-trip identically.
Use `as_value()` when assigning from an Enum.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, TypedDict

# --------------------------------------------------------------------------- #
# Hard limits imposed by external platforms (referenced by nodes, never inline)
# --------------------------------------------------------------------------- #

#: LinkedIn connection notes are truncated by the platform beyond ~300 chars.
#: N4 hard-enforces this and re-drafts rather than shipping a truncated note.
MAX_LINKEDIN_CONNECTION_NOTE_CHARS = 300

#: Free-tier ceilings enforced in code (see integrations/*). Kept here so the
#: numbers live in exactly one place and tests can assert against them.
FREE_TIER_LIMITS: dict[str, int] = {
    "hunter_lookups_per_month": 25,
    "gmail_sends_per_day": 500,
    "brevo_sends_per_day": 300,
    "deepl_chars_per_month": 500_000,
}


# --------------------------------------------------------------------------- #
# Enums + their Literal counterparts
# --------------------------------------------------------------------------- #

class Region(str, Enum):
    """Supported regions. `region` on the state is one of these codes."""

    US = "US"
    UK = "UK"
    EU = "EU"
    CA = "CA"          # Canada
    AU = "AU"          # Australia
    ME = "ME"          # Middle East (UAE / Saudi)


RegionValue = Literal["US", "UK", "EU", "CA", "AU", "ME"]
REGION_VALUES: frozenset[str] = frozenset(r.value for r in Region)


class Channel(str, Enum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    BOTH = "both"


ChannelValue = Literal["email", "linkedin", "both"]


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"


ApprovalStatusValue = Literal["pending", "approved", "edited", "rejected"]

#: Statuses permitted to proceed past N5 into the suppression gate.
#: `edited` counts as approved -- the human rewrote it and thereby signed off.
APPROVED_STATUSES: frozenset[str] = frozenset({"approved", "edited"})


class SuppressionStatus(str, Enum):
    CLEAR = "clear"
    BLOCKED_OPTOUT = "blocked_optout"
    BLOCKED_COMPLIANCE = "blocked_compliance"


SuppressionStatusValue = Literal["clear", "blocked_optout", "blocked_compliance"]


class SendStatus(str, Enum):
    NOT_SENT = "not_sent"
    SENT = "sent"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    PENDING_MANUAL_SEND = "pending_manual_send"   # N6b LinkedIn queue


SendStatusValue = Literal[
    "not_sent", "sent", "failed", "rate_limited", "pending_manual_send"
]


class ReplyCategory(str, Enum):
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    OBJECTION = "objection"
    OUT_OF_OFFICE = "out_of_office"
    NO_REPLY = "no_reply"


ReplyCategoryValue = Literal[
    "interested", "not_interested", "objection", "out_of_office", "no_reply"
]

#: Reply categories that MUST auto-add the contact to the tenant suppression
#: list (N5.5 rule 3). `objection` is deliberately excluded -- an objection is a
#: sales conversation, not an opt-out -- but N7 additionally scans reply text in
#: ANY category for explicit opt-out language, which does suppress.
AUTO_SUPPRESS_CATEGORIES: frozenset[str] = frozenset({"not_interested"})

#: Any reply in these categories immediately exits the automated cadence (N8).
EXITS_SEQUENCE_CATEGORIES: frozenset[str] = frozenset(
    {"interested", "not_interested"}
)


# --------------------------------------------------------------------------- #
# Draft message structs
# --------------------------------------------------------------------------- #

class EmailDraft(TypedDict, total=False):
    """Draft for `channel in ('email', 'both')`."""

    subject: str
    body: str


class LinkedInDraft(TypedDict, total=False):
    """
    Draft for `channel in ('linkedin', 'both')`.

    `connection_note` is hard-capped at MAX_LINKEDIN_CONNECTION_NOTE_CHARS by
    N4; `followup_dm` is optional and only used once a connection is accepted.
    """

    connection_note: str
    followup_dm: str


class DraftMessage(TypedDict, total=False):
    """
    Channel-keyed container for drafts.

    A lead on `channel == 'both'` carries an email draft *and* a LinkedIn draft
    simultaneously, so drafts are keyed by channel rather than stored as a bare
    string. `signal_referenced` records which item from `signals` the copy was
    built around -- N4's self-check asserts it is non-empty and actually appears
    in `signals`, which is how "never generic" is enforced mechanically.
    """

    email: EmailDraft
    linkedin: LinkedInDraft
    signal_referenced: str
    language: str            # BCP-47-ish tag the draft was localised into
    translated: bool         # True if translation.py rewrote the source copy


# --------------------------------------------------------------------------- #
# The lead state
# --------------------------------------------------------------------------- #

class LeadState(TypedDict, total=False):
    """
    Per-lead graph state. `total=False` because LangGraph nodes return partial
    updates; required-field enforcement is done by `validate_lead_state()` at
    the specific boundaries that care, not by the type system.

    The first eight blocks are the Section 3 data contract, verbatim. The final
    block is additive operational plumbing that the node specs themselves
    require (N2 `unreachable`, N7 `sent_at` windowing, N3/N4/N8 manual-review
    routing, N9 audit) -- it never replaces or renames a contract field.
    """

    # -- Identity ----------------------------------------------------------- #
    tenant_id: str
    niche_id: str
    region: RegionValue
    language: str
    lead_id: str

    # -- Company ------------------------------------------------------------ #
    company_name: str
    website: str
    linkedin_url: str
    location: str
    industry: str

    # -- Contact ------------------------------------------------------------ #
    contact_email: str
    contact_name: str

    # -- Research ----------------------------------------------------------- #
    signals: list[str]

    # -- Qualification ------------------------------------------------------ #
    fit_score: int          # 0-100
    fit_reason: str

    # -- Outreach ----------------------------------------------------------- #
    channel: ChannelValue
    draft_message: DraftMessage
    approval_status: ApprovalStatusValue
    suppression_status: SuppressionStatusValue
    send_status: SendStatusValue

    # -- Sequencing --------------------------------------------------------- #
    sequence_step: int
    next_action: str

    # -- Replies ------------------------------------------------------------ #
    reply_text: str
    reply_category: ReplyCategoryValue

    # -- Meta --------------------------------------------------------------- #
    last_updated: datetime

    # -- Operational (additive; see class docstring) ------------------------- #
    source: str                 # google_places | osm | apollo | csv | fixture
    discovered_at: datetime
    dedupe_key: str             # domain, else normalised name+location
    unreachable: bool           # N2: no email AND no linkedin_url -> skip downstream
    needs_manual_review: bool   # N3/N4 double-failure, or any retry-exhausted node
    manual_review_reason: str
    sent_at: datetime | None    # N7 matches replies inside a window after this
    last_touch_at: datetime | None
    next_touch_due: datetime | None   # N8 schedules the next cadence step
    archived: bool
    archive_reason: str         # below_threshold | rejected | suppressed | cadence_exhausted
    dry_run: bool               # N6a/N6b print instead of send when True
    errors: list[dict[str, Any]]   # append-only {node, error, at} audit trail


# --------------------------------------------------------------------------- #
# Batch wrapper
# --------------------------------------------------------------------------- #

class BatchTarget(TypedDict):
    """One `(niche_id, region)` pair resolved by N0 for this batch."""

    niche_id: str
    region: RegionValue
    language: str


class LeadBatch(TypedDict, total=False):
    """
    Container the CLI fans out from. The compiled graph is
    `StateGraph(LeadState)` -- i.e. it runs *per lead*, one thread per lead_id,
    which is what makes the N5 interrupt resumable per lead rather than blocking
    a whole batch. LeadBatch lives outside the graph: it is what
    `cli/run_batch.py` builds, iterates, and reports on.
    """

    tenant_id: str
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    dry_run: bool
    targets: list[BatchTarget]
    leads: list[LeadState]
    low_yield_targets: list[BatchTarget]   # N1: discovery under the min threshold
    needs_manual_review: list[str]         # lead_ids
    archived: list[str]                    # lead_ids
    errors: list[dict[str, Any]]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def utcnow() -> datetime:
    """Timezone-aware UTC now. The only clock this system reads."""
    return datetime.now(timezone.utc)


def as_value(v: Any) -> Any:
    """Coerce an Enum to its plain string value; pass anything else through."""
    return v.value if isinstance(v, Enum) else v


def normalise_domain(website: str | None) -> str:
    """
    Reduce a URL to a bare domain for dedupe:
    'https://WWW.Acme.co.uk/about?x=1' -> 'acme.co.uk'
    """
    if not website:
        return ""
    s = website.strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    return s.strip().strip(".")


def make_dedupe_key(
    company_name: str = "",
    location: str = "",
    website: str = "",
) -> str:
    """
    N1 dedupe key. Domain wins when we have one (most reliable); otherwise fall
    back to a normalised name+location pair.
    """
    domain = normalise_domain(website)
    if domain:
        return f"domain:{domain}"
    name = " ".join((company_name or "").lower().split())
    loc = " ".join((location or "").lower().split())
    return f"name:{name}|loc:{loc}"


def make_lead_id(tenant_id: str, dedupe_key: str) -> str:
    """
    Deterministic, tenant-scoped lead id. Deterministic on purpose: re-running
    discovery for the same tenant must land on the same lead_id so checkpoints,
    CRM rows and suppression entries line up instead of duplicating.
    """
    digest = hashlib.sha1(f"{tenant_id}::{dedupe_key}".encode()).hexdigest()[:16]
    return f"{tenant_id}-{digest}"


def new_lead_state(
    *,
    tenant_id: str,
    niche_id: str,
    region: RegionValue | Region,
    language: str = "en",
    company_name: str = "",
    website: str = "",
    linkedin_url: str = "",
    location: str = "",
    industry: str = "",
    contact_email: str = "",
    contact_name: str = "",
    source: str = "unknown",
    dry_run: bool = False,
    **extra: Any,
) -> LeadState:
    """
    Build a fully-defaulted LeadState. Every node may assume these keys exist,
    which removes a whole class of `.get(...) or default` noise downstream.
    """
    dedupe_key = make_dedupe_key(company_name, location, website)
    now = utcnow()
    state: LeadState = {
        "tenant_id": tenant_id,
        "niche_id": niche_id,
        "region": as_value(region),
        "language": language,
        "lead_id": make_lead_id(tenant_id, dedupe_key),
        "company_name": company_name,
        "website": website,
        "linkedin_url": linkedin_url,
        "location": location,
        "industry": industry,
        "contact_email": contact_email,
        "contact_name": contact_name,
        "signals": [],
        "fit_score": 0,
        "fit_reason": "",
        "channel": Channel.EMAIL.value,
        "draft_message": {},
        "approval_status": ApprovalStatus.PENDING.value,
        "suppression_status": SuppressionStatus.CLEAR.value,
        "send_status": SendStatus.NOT_SENT.value,
        "sequence_step": 0,
        "next_action": "",
        "reply_text": "",
        "reply_category": ReplyCategory.NO_REPLY.value,
        "last_updated": now,
        "source": source,
        "discovered_at": now,
        "dedupe_key": dedupe_key,
        "unreachable": False,
        "needs_manual_review": False,
        "manual_review_reason": "",
        "sent_at": None,
        "last_touch_at": None,
        "next_touch_due": None,
        "archived": False,
        "archive_reason": "",
        "dry_run": dry_run,
        "errors": [],
    }
    state.update(extra)  # type: ignore[typeddict-item]
    return state


def touch(**updates: Any) -> dict[str, Any]:
    """
    Wrap a node's partial state update so `last_updated` is always refreshed.
    Nodes should `return touch(fit_score=..., fit_reason=...)`.
    """
    updates["last_updated"] = utcnow()
    return updates


def record_error(state: LeadState, node_name: str, error: Any) -> list[dict[str, Any]]:
    """
    Return the lead's error list with one entry appended. Section 8: a node
    failure is data, not an exception that kills the batch.
    """
    if isinstance(error, BaseException):
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = str(error)
    entry = {"node": node_name, "error": detail, "at": utcnow().isoformat()}
    return [*(state.get("errors") or []), entry]


def is_reachable(state: LeadState) -> bool:
    """A lead is reachable if we have an email OR a LinkedIn URL (N2)."""
    return bool(state.get("contact_email") or state.get("linkedin_url"))


def wants_email(channel: str | Channel) -> bool:
    return as_value(channel) in ("email", "both")


def wants_linkedin(channel: str | Channel) -> bool:
    return as_value(channel) in ("linkedin", "both")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

#: Keys a lead must carry the moment it leaves N1 (Discovery).
REQUIRED_AFTER_DISCOVERY: tuple[str, ...] = (
    "tenant_id", "niche_id", "region", "lead_id", "company_name",
)

_ENUM_FIELDS: dict[str, type[Enum]] = {
    "region": Region,
    "channel": Channel,
    "approval_status": ApprovalStatus,
    "suppression_status": SuppressionStatus,
    "send_status": SendStatus,
    "reply_category": ReplyCategory,
}


def validate_lead_state(
    state: LeadState,
    *,
    required: tuple[str, ...] = REQUIRED_AFTER_DISCOVERY,
) -> list[str]:
    """
    Return a list of human-readable problems with `state`; empty means valid.

    Returns rather than raises so a caller can route one bad lead to
    needs_manual_review without taking the batch down (Section 8).
    """
    problems: list[str] = []

    for key in required:
        if not state.get(key):
            problems.append(f"missing required field: {key}")

    for key, enum_cls in _ENUM_FIELDS.items():
        if key not in state:
            continue
        value = as_value(state[key])  # type: ignore[literal-required]
        allowed = {m.value for m in enum_cls}
        if value not in allowed:
            problems.append(f"{key}={value!r} is not one of {sorted(allowed)}")

    score = state.get("fit_score")
    if score is not None:
        if not isinstance(score, int) or isinstance(score, bool):
            problems.append(f"fit_score must be an int, got {type(score).__name__}")
        elif not 0 <= score <= 100:
            problems.append(f"fit_score={score} out of range 0-100")

    step = state.get("sequence_step")
    if step is not None and (
        not isinstance(step, int) or isinstance(step, bool) or step < 0
    ):
        problems.append(f"sequence_step must be a non-negative int, got {step!r}")

    signals = state.get("signals")
    if signals is not None and not isinstance(signals, list):
        problems.append(f"signals must be a list, got {type(signals).__name__}")

    note = ((state.get("draft_message") or {}).get("linkedin") or {}).get(
        "connection_note"
    )
    if note and len(note) > MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
        problems.append(
            f"linkedin connection_note is {len(note)} chars, "
            f"max {MAX_LINKEDIN_CONNECTION_NOTE_CHARS}"
        )

    ts = state.get("last_updated")
    if isinstance(ts, datetime) and ts.tzinfo is None:
        problems.append("last_updated must be timezone-aware (use utcnow())")

    return problems


# --------------------------------------------------------------------------- #
# CRM projection (N9) -- the Looker Studio column contract
# --------------------------------------------------------------------------- #

#: Exact Google Sheet / Airtable header order. One row per lead, plain strings,
#: ISO-8601 timestamps -- so Looker Studio can be pointed straight at the sheet.
CRM_COLUMNS: tuple[str, ...] = (
    "lead_id", "tenant_id", "niche_id", "region", "language",
    "company_name", "website", "linkedin_url", "location", "industry",
    "contact_name", "contact_email",
    "fit_score", "fit_reason", "signals",
    "channel", "approval_status", "suppression_status", "send_status",
    "sequence_step", "next_action",
    "reply_category", "reply_text",
    "unreachable", "needs_manual_review", "archived", "archive_reason",
    "source", "discovered_at", "sent_at", "last_touch_at", "next_touch_due",
    "last_updated",
)


def _cell(value: Any) -> str:
    """Flatten one state value into a Sheets/Looker-friendly scalar string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (list, tuple)):
        return " | ".join(str(as_value(v)) for v in value)
    return str(as_value(value))


def lead_to_crm_row(state: LeadState) -> list[str]:
    """Project a LeadState onto CRM_COLUMNS, in order."""
    return [_cell(state.get(col)) for col in CRM_COLUMNS]


def lead_to_crm_dict(state: LeadState) -> dict[str, str]:
    """Same projection, keyed -- for the Airtable backend."""
    return {col: _cell(state.get(col)) for col in CRM_COLUMNS}
