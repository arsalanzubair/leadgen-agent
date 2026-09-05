"""
N7 -- Reply Monitoring and Classification.

In:  contact_email, sent_at
Out: reply_text, reply_category

Polls email through EmailReader (Gmail API or IMAP), matching on contact_email
within a window after sent_at. LinkedIn replies are logged manually against a
lead_id through `log_manual_reply` (exposed on the CLI), because there is no
LinkedIn read API we are willing to use.

OUT-OF-OFFICE DETECTION RUNS BEFORE CLASSIFICATION
--------------------------------------------------
An autoreply is not a reply. If it reached the classifier it would frequently
score as `interested` ("I will get back to you"), which would pull the lead out
of its cadence and put it in the operator's same-day follow-up list for nothing.
So `detect_out_of_office` runs first, on headers (RFC 3834 `Auto-Submitted`)
and then on the text, and short-circuits.

Any reply that requires suppression is passed to N5.5's `auto_suppress_from_reply`
IMMEDIATELY -- at classification time, not at the next send attempt.

Categories: interested | not_interested | objection | out_of_office | no_reply
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from src.integrations import email_reader, llm, suppression
from src.providers import llm_for, reader_for
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.nodes.n5_5_suppression_gate import auto_suppress_from_reply
from src.reliability import log, node
from src.state import EXITS_SEQUENCE_CATEGORIES, LeadState, ReplyCategory, utcnow

#: Phrases that identify an out-of-office autoreply, across the languages this
#: system sends in. Matched against the reply text with the quoted thread
#: already stripped.
OOO_PHRASES: tuple[str, ...] = (
    # English
    "out of the office", "out of office", "outofoffice", "on annual leave",
    "on holiday", "on vacation", "away from my desk", "away from the office",
    "i am currently away", "currently away", "on parental leave",
    "on maternity leave", "on paternity leave", "on sick leave",
    "limited access to email", "limited access to my email",
    "will be back on", "i return on", "returning on", "back in the office on",
    "automatic reply", "auto-reply", "autoreply", "thank you for your email",
    "no longer with", "has left the company", "i will respond when i return",
    # French
    "absent du bureau", "en congé", "en conges", "de retour le",
    "réponse automatique", "reponse automatique",
    # German
    "abwesend", "nicht im büro", "nicht im buero", "im urlaub",
    "automatische antwort", "abwesenheitsnotiz",
    # Spanish / Italian / Dutch
    "fuera de la oficina", "de vacaciones", "fuori sede", "afwezig",
    # Arabic
    "خارج المكتب", "في إجازة",
)

#: Subject-line prefixes mail systems use for autoreplies.
OOO_SUBJECT_PREFIXES: tuple[str, ...] = (
    "out of office", "automatic reply", "auto-reply", "autoreply",
    "abwesenheit", "réponse automatique", "reponse automatique",
    "respuesta automática", "afwezigheid",
)

SYSTEM_PROMPT = (
    "You classify replies to cold outreach emails. You are literal and "
    "conservative: you classify what the person actually wrote, not what the "
    "sender hopes they meant. A polite brush-off is not interest. A question "
    "about pricing or timing IS interest."
)

PROMPT_TEMPLATE = """\
Classify this reply to a cold outreach email.

# What we sent them
We contacted {company_name} about: {signal}

# Their reply
{reply_text}

# Categories
- interested:     they want to talk, ask a question that moves it forward,
                  request a call, ask about pricing or timing, or forward it to
                  a colleague who should talk to us.
- not_interested: they decline, say no, ask to be removed, say it is not
                  relevant, or say they already have a solution and are happy.
- objection:      they push back with a specific concern (price, timing,
                  incumbent vendor, scepticism) but the conversation is not
                  closed. An objection is a live conversation, not a rejection.
- out_of_office:  an automatic absence reply.

Return JSON only:
{{"category": "<one of the four>", "confidence": <0-100>, \
"reason": "<one short sentence quoting the words that decided it>"}}
"""


# --------------------------------------------------------------------------- #
# Out-of-office detection
# --------------------------------------------------------------------------- #

def detect_out_of_office(
    text: str, *, subject: str = "", message: email_reader.IncomingMessage | None = None
) -> tuple[bool, str]:
    """
    Is this an autoreply rather than a real reply?

    Three checks, cheapest and most reliable first:
      1. RFC 3834 / vendor headers, when we have the message object;
      2. subject-line prefixes mail systems add;
      3. body phrases, in every language this system sends in.

    Returns (is_ooo, reason).
    """
    if message is not None and message.auto_submitted:
        return True, "Auto-Submitted header marks this machine-generated"

    subject_lower = (subject or "").lower()
    for prefix in OOO_SUBJECT_PREFIXES:
        if prefix in subject_lower:
            return True, f"subject contains {prefix!r}"

    body_lower = (text or "").lower()
    for phrase in OOO_PHRASES:
        if phrase in body_lower:
            return True, f"body contains {phrase!r}"

    return False, ""


# --------------------------------------------------------------------------- #
# Deterministic offline classifier (the `mock` provider)
# --------------------------------------------------------------------------- #

_INTERESTED_MARKERS = (
    "yes", "sure", "sounds good", "happy to", "would like", "interested",
    "let's talk", "lets talk", "book", "call", "meeting", "chat", "thursday",
    "how much", "pricing", "what would it cost", "send me", "tell me more",
    "worth a", "keen", "forwarded", "the right person",
)
_NOT_INTERESTED_MARKERS = (
    "not interested", "no thanks", "no thank you", "not relevant", "remove me",
    "unsubscribe", "we're all set", "we are all set", "happy with", "pass",
    "not at this time", "not for us", "stop",
    "do not contact", "don't contact", "do not email", "don't email",
    "stop contacting", "stop emailing", "take me off",
)
_OBJECTION_MARKERS = (
    "too expensive", "already have", "already using", "we use", "budget",
    "not the right time", "later in the year", "who are you", "how did you get",
    "where did you get", "sceptical", "skeptical", "prove", "case study",
    "gdpr", "legal basis",
)


def classify_offline(reply_text: str) -> tuple[str, int, str]:
    """
    Keyword classifier used when no LLM provider is configured.

    Ordering is deliberate: an explicit refusal outranks an enthusiasm word, so
    "no thanks, but this looks interesting" classifies as not_interested rather
    than interested. Getting that backwards keeps mailing someone who said no.
    """
    text = (reply_text or "").lower()
    if not text.strip():
        return ReplyCategory.NO_REPLY.value, 100, "empty reply"

    # Explicit opt-out language IS a refusal, in any of the languages the
    # suppression module knows -- checked first so a phrase this module's own
    # English keyword list happens to miss still classifies correctly.
    if suppression.contains_opt_out(text):
        return ReplyCategory.NOT_INTERESTED.value, 90, "explicit opt-out language"

    for marker in _NOT_INTERESTED_MARKERS:
        if marker in text:
            return ReplyCategory.NOT_INTERESTED.value, 85, f"contains {marker!r}"
    for marker in _OBJECTION_MARKERS:
        if marker in text:
            return ReplyCategory.OBJECTION.value, 70, f"contains {marker!r}"
    for marker in _INTERESTED_MARKERS:
        if marker in text:
            return ReplyCategory.INTERESTED.value, 75, f"contains {marker!r}"
    return ReplyCategory.OBJECTION.value, 40, "reply received but unclear; treat as a live conversation"


def _mock_classifier(prompt: str, context: dict) -> str:
    category, confidence, reason = classify_offline(context.get("reply_text", ""))
    return json.dumps({"category": category, "confidence": confidence, "reason": reason})


llm.register_mock("reply_classification", _mock_classifier)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

VALID_CATEGORIES = {c.value for c in ReplyCategory}


def classify_reply(
    reply_text: str, state: LeadState, writer: Any | None = None
) -> tuple[str, str]:
    """
    Classify a real (non-autoreply) reply. Returns (category, reason).

    A failure here degrades to `objection` rather than raising: an unclassified
    reply is a live conversation that a human should look at, and treating it
    as `no_reply` would keep the automated cadence running at someone who
    actually wrote back.
    """
    signal = (state.get("draft_message") or {}).get("signal_referenced", "") or \
        (state.get("signals") or [""])[0]

    try:
        parsed, _ = (writer or llm_for(None)).complete_json(
            PROMPT_TEMPLATE.format(
                company_name=state.get("company_name", ""),
                signal=signal or "(no specific signal recorded)",
                reply_text=reply_text[:4000],
            ),
            task="reply_classification",
            system=SYSTEM_PROMPT,
            temperature=0.0,
            context={"reply_text": reply_text, "state": state},
            required_keys=("category",),
        )
        category = str(parsed.get("category", "")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()
        if category not in VALID_CATEGORIES:
            raise ValueError(f"model returned unknown category {category!r}")
        return category, reason
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "reply classification failed for lead=%s (%s); treating as an objection "
            "so a human reviews it", state.get("lead_id"), exc,
        )
        return ReplyCategory.OBJECTION.value, f"classification failed: {exc}"


# --------------------------------------------------------------------------- #
# Manual LinkedIn reply entry
# --------------------------------------------------------------------------- #

def log_manual_reply(
    state: LeadState, reply_text: str, writer: Any | None = None
) -> dict[str, Any]:
    """
    Record a reply the operator read on LinkedIn (or anywhere else) by hand.

    Goes through exactly the same OOO check, classifier and auto-suppression
    path as an emailed reply, so a "please stop contacting me" typed in from
    LinkedIn suppresses the contact just as reliably.

    `writer` defaults to None, which resolves the workspace's model from its
    configuration. This is called from the CLI, which has a tenant id but no
    loaded config to hand in.
    """
    is_ooo, ooo_reason = detect_out_of_office(reply_text)
    if is_ooo:
        return {
            "reply_text": reply_text,
            "reply_category": ReplyCategory.OUT_OF_OFFICE.value,
            "next_action": f"out of office ({ooo_reason}); cadence deferred",
        }

    category, reason = classify_reply(reply_text, state, writer)
    updates: dict[str, Any] = {
        "reply_text": reply_text,
        "reply_category": category,
        "next_action": f"reply classified {category}: {reason}",
    }
    updates.update(auto_suppress_from_reply({**state, **updates}))
    return updates


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n7_reply_monitoring")
def n7_reply_monitoring(
    state: LeadState, *, tenant_config: TenantConfig | None = None
) -> dict:
    """
    Look for a reply to this lead's most recent touch and classify it.

    A lead with nothing sent yet, or a LinkedIn-only lead, comes back
    `no_reply` -- N8 then decides whether the next touch is due.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])

    # A reply already recorded (manual entry, or a previous run) is not
    # re-fetched or re-classified.
    if state.get("reply_text"):
        return {}

    writer = llm_for(config, tenant_id=state["tenant_id"])

    sent_at = state.get("sent_at")
    contact_email = (state.get("contact_email") or "").strip()
    if not sent_at or not contact_email:
        return {"reply_category": ReplyCategory.NO_REPLY.value}

    lookback = int(config.runtime.get("reply_lookback_days", 14))
    since = max(sent_at, utcnow() - timedelta(days=lookback))

    reader = reader_for(
        config, tenant_id=state["tenant_id"], dry_run=bool(state.get("dry_run"))
    )
    messages = reader.fetch_replies(contact_email, since)
    if not messages:
        return {"reply_category": ReplyCategory.NO_REPLY.value}

    message = messages[0]
    body = email_reader.strip_quoted_reply(message.body)

    # -- out of office FIRST, before the classifier ever sees it ------------ #
    is_ooo, ooo_reason = detect_out_of_office(
        body, subject=message.subject, message=message
    )
    if is_ooo:
        log.info(
            "out-of-office autoreply tenant=%s lead=%s (%s); not counted as a reply",
            state.get("tenant_id"), state.get("lead_id"), ooo_reason,
        )
        return {
            "reply_text": body[:2000],
            "reply_category": ReplyCategory.OUT_OF_OFFICE.value,
            "next_action": f"out of office ({ooo_reason}); cadence deferred",
        }

    category, reason = classify_reply(body, state, writer)
    log.info(
        "reply tenant=%s lead=%s company=%r category=%s (%s)",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        category, reason,
    )

    updates: dict[str, Any] = {
        "reply_text": body[:2000],
        "reply_category": category,
        "next_action": f"reply classified {category}: {reason}",
    }

    # Auto-suppression happens NOW, not at the next send attempt.
    updates.update(auto_suppress_from_reply({**state, **updates}))
    return updates


def route_after_reply(state: LeadState) -> str:
    """
    Section 5: after N7, `interested` goes to the manual handoff plus N9;
    everything else goes to N8.
    """
    category = state.get("reply_category", ReplyCategory.NO_REPLY.value)
    if category == ReplyCategory.INTERESTED.value:
        return "manual_handoff"
    if state.get("archived") or state.get("suppression_status") != "clear":
        return "crm"
    return "sequencer"


def has_replied(state: LeadState) -> bool:
    """True if this lead has given a real reply that ends the sequence."""
    return state.get("reply_category", "") in EXITS_SEQUENCE_CATEGORIES
