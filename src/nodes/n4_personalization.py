"""
N4 -- Personalization.

In:  LeadState with signals and channel, plus tenant tone and sending_identity
Out: draft_message (email: subject + body; linkedin: connection_note and an
     optional followup_dm)

Three requirements from Section 4 drive the whole design here:

  1. The draft must reference a SPECIFIC signal, never generic copy. This is
     enforced mechanically, not by asking the prompt nicely: `verify_references_signal`
     checks the draft's own words against the lead's `signals`. Two failures
     flag the lead for manual drafting rather than passing a generic message
     forward.
  2. LinkedIn connection notes are HARD-capped at 300 characters. An
     over-length draft is re-prompted to be shorter and re-checked; it is never
     truncated mid-sentence, because a truncated note is worse than no note.
  3. Localisation via integrations/translation.py whenever the tenant's
     language_map gives a non-English language for the region.

N4 is also re-entered by N8 for each subsequent cadence touch, so it takes the
current `sequence_step` into account and drafts THAT step's message.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.integrations import llm, translation
from src.providers import llm_for, translation_for
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.state import (
    MAX_LINKEDIN_CONNECTION_NOTE_CHARS,
    DraftMessage,
    LeadState,
    wants_email,
    wants_linkedin,
)

SYSTEM_PROMPT = (
    "You write cold outreach that busy people actually reply to. You open with "
    "a specific, verifiable observation about the recipient's business -- never "
    "a compliment, never a generic opener. You write short, plain sentences. "
    "You never claim a relationship, a referral, or knowledge you do not have. "
    "You never use the words 'synergy', 'leverage', 'revolutionary' or "
    "'game-changer'."
)

EMAIL_PROMPT = """\
Write touch {step} of a cold email sequence.

# Who is writing
{from_name} at {company_name}{website_line}

# Voice
{tone}

# Who is receiving it
Company: {lead_company}
Contact: {contact_name}
Industry: {industry}
Location: {location}

# What we actually observed about them (use ONE of these, specifically)
{signals}

# This touch
{step_intent}

# Rules
- Maximum {max_words} words in the body.
- Subject line: maximum {subject_max_chars} characters, lowercase-ish, no
  clickbait, no "quick question", no company name stuffing.
- Open by referencing ONE specific observation from the list above, quoting the
  concrete detail. Do not paraphrase it into something vague.
- One idea. One question. No bullet lists, no links, no attachments.
- Do not mention that this is an automated or sequenced message.
- Never use any of these phrases: {forbidden}
- Sign off with the first name only.
- Do NOT include an unsubscribe or opt-out line; it is appended separately.

Return JSON only:
{{"subject": "...", "body": "...", "signal_referenced": "<the exact observation \
from the list above that you used>"}}
"""

LINKEDIN_PROMPT = """\
Write a LinkedIn connection note and a follow-up DM.

# Who is writing
{from_name} at {company_name}

# Voice
{tone}

# Who is receiving it
Company: {lead_company}
Contact: {contact_name}
Industry: {industry}

# What we actually observed about them (use ONE of these, specifically)
{signals}

# This touch
{step_intent}

# Rules
- The connection note MUST be {max_chars} characters or fewer. This is a hard
  platform limit; a note over the limit is unusable.
- The connection note makes NO ask and includes NO link. One specific
  observation, one reason you are connecting.
- The follow-up DM is sent only after they accept. Maximum 90 words. It may ask
  one question. Still no link.
- Never use any of these phrases: {forbidden}
- Do not mention automation or sequences.

Return JSON only:
{{"connection_note": "...", "followup_dm": "...", "signal_referenced": "<the \
exact observation from the list above that you used>"}}
"""


# --------------------------------------------------------------------------- #
# The self-check: does the draft actually reference a specific signal?
# --------------------------------------------------------------------------- #

_STOPWORDS = frozenset({
    "with", "that", "this", "from", "have", "your", "their", "there", "about",
    "which", "would", "could", "should", "been", "being", "into", "over",
    "under", "more", "most", "some", "than", "then", "they", "them", "when",
    "what", "were", "will", "just", "like", "only", "also", "other", "page",
    "site", "website", "business", "company", "still", "after", "before",
})


def _content_words(text: str) -> set[str]:
    return {
        word for word in re.split(r"\W+", (text or "").lower())
        if len(word) > 3 and word not in _STOPWORDS
    }


def verify_references_signal(
    draft_text: str, signals: list[str], *, claimed: str = ""
) -> tuple[bool, str]:
    """
    Does `draft_text` demonstrably reference one of `signals`?

    Returns (ok, matched_signal). The test is lexical overlap against each
    signal's distinctive words: a draft that says "your booking page sends
    people to a phone number" genuinely overlaps "no online booking - phone
    number only on the contact page", while "I came across your practice" does
    not overlap anything.

    The model's own `signal_referenced` claim is checked first but is NOT
    trusted on its own -- a model will happily claim it referenced a signal it
    ignored, which is exactly the failure this guard exists to catch.
    """
    if not signals:
        return False, ""
    draft_words = _content_words(draft_text)
    if not draft_words:
        return False, ""

    ordered = list(signals)
    if claimed:
        # Check the claimed signal first, but by the same evidence standard.
        ordered = [s for s in signals if s == claimed] + [s for s in signals if s != claimed]
        if claimed not in signals:
            ordered = [claimed, *ordered]

    for signal in ordered:
        signal_words = _content_words(signal)
        if not signal_words:
            continue
        overlap = draft_words & signal_words
        needed = 2 if len(signal_words) >= 4 else 1
        if len(overlap) >= needed:
            return True, signal
    return False, ""


def _forbidden_hits(text: str, forbidden: list[str]) -> list[str]:
    lowered = (text or "").lower()
    return [phrase for phrase in forbidden if phrase.lower() in lowered]


# --------------------------------------------------------------------------- #
# Deterministic offline drafter (the `mock` provider)
# --------------------------------------------------------------------------- #

def draft_offline(state: LeadState, channel: str, step_intent: str) -> dict[str, Any]:
    """
    Build a draft with no LLM, for dry runs on a clone with no API keys.

    It composes real copy from the lead's own first signal, so the self-check,
    the 300-character cap, the forbidden-phrase check and the translation path
    are all genuinely exercised -- and a lead with NO signals produces a
    generic draft that the self-check correctly rejects, which is what the
    fixture's Grantly entry is there to prove.
    """
    signals = [str(s) for s in (state.get("signals") or [])]
    company = state.get("company_name", "your team")
    first_name = (state.get("contact_name") or "").split(" ")[0]
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    if not signals:
        # Deliberately generic -- the self-check must catch this.
        body = (
            f"{greeting}\n\nI came across {company} and thought I would get in "
            "touch about what we do.\n\nWorth a conversation?\n"
        )
        return {
            "subject": f"{company}",
            "body": body,
            "connection_note": f"Hi {first_name or 'there'} - would like to connect.",
            "followup_dm": "Thanks for connecting. Would you be open to a chat?",
            "signal_referenced": "",
        }

    signal = signals[0]
    body = (
        f"{greeting}\n\n"
        f"I was looking at {company} and noticed {signal}.\n\n"
        "In practices like yours that usually means enquiries arriving outside "
        "opening hours never get answered.\n\n"
        "Worth a short conversation about what that is costing?\n"
    )
    note = f"Hi {first_name or 'there'} - noticed {signal} at {company}. Happy to connect."
    if len(note) > MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
        note = note[: MAX_LINKEDIN_CONNECTION_NOTE_CHARS - 1].rsplit(" ", 1)[0] + "."
    subject = f"{company} - {signal}"
    if len(subject) > 60:
        subject = subject[:57].rstrip(" -") + "..."
    return {
        "subject": subject,
        "body": body,
        "connection_note": note,
        "followup_dm": (
            f"Thanks for connecting. The reason I reached out: {signal}. "
            "Is that something you are already working on?"
        ),
        "signal_referenced": signal,
    }


def _mock_email(prompt: str, context: dict) -> str:
    draft = draft_offline(context["state"], "email", context.get("step_intent", ""))
    return json.dumps({
        "subject": draft["subject"],
        "body": draft["body"],
        "signal_referenced": draft["signal_referenced"],
    })


def _mock_linkedin(prompt: str, context: dict) -> str:
    draft = draft_offline(context["state"], "linkedin", context.get("step_intent", ""))
    return json.dumps({
        "connection_note": draft["connection_note"],
        "followup_dm": draft["followup_dm"],
        "signal_referenced": draft["signal_referenced"],
    })


llm.register_mock("personalization_email", _mock_email)
llm.register_mock("personalization_linkedin", _mock_linkedin)


# --------------------------------------------------------------------------- #
# Drafting
# --------------------------------------------------------------------------- #

def _signals_block(signals: list[str]) -> str:
    if not signals:
        return "(nothing specific was found - say so rather than inventing something)"
    return "\n".join(f"- {s}" for s in signals)


def _step_intent(config: TenantConfig, channel: str, step: int) -> tuple[str, dict[str, Any]]:
    """The cadence step's `intent` text and its limits, for this touch."""
    cadence = config.cadence_for(channel if channel != "both" else "both")
    steps = cadence.get("steps", [])
    for entry in steps:
        if int(entry.get("step", 0)) == max(1, step):
            return str(entry.get("intent", "")).strip(), entry
    return (str(steps[0].get("intent", "")).strip(), steps[0]) if steps else ("", {})


def draft_email(
    state: LeadState, config: TenantConfig, step: int, writer: Any | None = None
) -> tuple[dict[str, str], str]:
    """
    Draft one email for this lead at this step of the cadence.

    `writer` is the workspace's model, resolved once by the node and passed in
    so a re-prompt does not rebuild the adapter. Left as None it resolves from
    `config`, which is what a direct caller or a test wants.
    """
    personalization = config.personalization
    intent, step_cfg = _step_intent(config, "email", step)
    identity = config.sending_identity
    website = identity.get("website", "")

    prompt = EMAIL_PROMPT.format(
        step=max(1, step),
        from_name=identity.get("from_name", ""),
        company_name=identity.get("company_name", ""),
        website_line=f" ({website})" if website else "",
        tone=config.tone,
        lead_company=state.get("company_name", ""),
        contact_name=state.get("contact_name") or "(name unknown - do not guess one)",
        industry=state.get("industry") or "(unknown)",
        location=state.get("location") or "(unknown)",
        signals=_signals_block([str(s) for s in state.get("signals") or []]),
        step_intent=intent or "Opening message.",
        max_words=step_cfg.get("max_words", personalization.get("email_max_words", 120)),
        subject_max_chars=personalization.get("subject_max_chars", 60),
        forbidden=", ".join(personalization.get("forbidden_phrases", [])) or "(none)",
    )

    parsed, _ = (writer or llm_for(config)).complete_json(
        prompt,
        task="personalization_email",
        system=SYSTEM_PROMPT,
        temperature=0.6,
        context={"state": state, "step_intent": intent},
        required_keys=("subject", "body"),
    )
    return (
        {
            "subject": str(parsed.get("subject", "")).strip(),
            "body": str(parsed.get("body", "")).strip(),
        },
        str(parsed.get("signal_referenced", "")).strip(),
    )


def draft_linkedin(
    state: LeadState, config: TenantConfig, step: int, writer: Any | None = None
) -> tuple[dict[str, str], str]:
    """Draft a connection note and follow-up DM. See `draft_email` on `writer`."""
    personalization = config.personalization
    intent, _ = _step_intent(config, "linkedin", step)
    identity = config.sending_identity

    prompt = LINKEDIN_PROMPT.format(
        from_name=identity.get("from_name", ""),
        company_name=identity.get("company_name", ""),
        tone=config.tone,
        lead_company=state.get("company_name", ""),
        contact_name=state.get("contact_name") or "(name unknown - do not guess one)",
        industry=state.get("industry") or "(unknown)",
        signals=_signals_block([str(s) for s in state.get("signals") or []]),
        step_intent=intent or "Connection request.",
        max_chars=MAX_LINKEDIN_CONNECTION_NOTE_CHARS,
        forbidden=", ".join(personalization.get("forbidden_phrases", [])) or "(none)",
    )

    parsed, _ = (writer or llm_for(config)).complete_json(
        prompt,
        task="personalization_linkedin",
        system=SYSTEM_PROMPT,
        temperature=0.6,
        context={"state": state, "step_intent": intent},
        required_keys=("connection_note",),
    )
    return (
        {
            "connection_note": str(parsed.get("connection_note", "")).strip(),
            "followup_dm": str(parsed.get("followup_dm", "")).strip(),
        },
        str(parsed.get("signal_referenced", "")).strip(),
    )


def shorten_note(
    note: str, state: LeadState, config: TenantConfig, writer: Any | None = None
) -> str:
    """
    Bring an over-length connection note under the 300-character limit by
    RE-PROMPTING, not by truncating.

    If the model still cannot do it, the caller treats it as a self-check
    failure and the lead goes to manual drafting -- a half-sentence connection
    note is worse than no outreach.
    """
    if len(note) <= MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
        return note

    log.info(
        "connection note is %d chars (limit %d); re-prompting for a shorter one",
        len(note), MAX_LINKEDIN_CONNECTION_NOTE_CHARS,
    )
    try:
        parsed, _ = (writer or llm_for(config)).complete_json(
            "Shorten this LinkedIn connection note to "
            f"{MAX_LINKEDIN_CONNECTION_NOTE_CHARS - 20} characters or fewer while "
            "keeping the specific observation it makes. Keep it natural; do not "
            "end mid-sentence.\n\n"
            f"Note ({len(note)} chars):\n{note}",
            task="personalization_linkedin",
            system=SYSTEM_PROMPT,
            temperature=0.3,
            context={"state": state, "step_intent": "shorten"},
            required_keys=("connection_note",),
        )
        shorter = str(parsed.get("connection_note", "")).strip()
        if shorter and len(shorter) <= MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
            return shorter
    except Exception as exc:  # noqa: BLE001
        log.warning("connection note shortening failed: %s", exc)
    return note


# --------------------------------------------------------------------------- #
# The compliance footer
# --------------------------------------------------------------------------- #

#: Per-language footer wording. Each entry MUST contain a phrase that appears in
#: the corresponding region's `optout_phrases` / `legal_basis_phrases` in
#: config/compliance_profiles.yaml -- these two files are a matched pair, and a
#: new language needs an entry in both.
FOOTER_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "optout": 'If this is not relevant, reply "unsubscribe" and I will not follow up.',
        "legal_basis": (
            "You are receiving this at a published business address on the basis of "
            "legitimate interest (GDPR Art. 6(1)(f)). Reply to object at any time."
        ),
    },
    "fr": {
        "optout": (
            "Pour ne plus recevoir de messages de ma part, répondez simplement "
            "à ce message."
        ),
        "legal_basis": (
            "Vous recevez ce message à une adresse professionnelle publiée, sur la "
            "base de l'intérêt légitime (RGPD art. 6.1.f). Répondez pour vous opposer."
        ),
    },
    "de": {
        "optout": (
            "Antworten Sie mit „abmelden“, wenn Sie keine weiteren Nachrichten "
            "erhalten möchten."
        ),
        "legal_basis": (
            "Sie erhalten diese Nachricht an eine veröffentlichte Geschäftsadresse "
            "auf Grundlage eines berechtigten Interesses (DSGVO Art. 6 Abs. 1 lit. f)."
        ),
    },
    "es": {
        "optout": "Para no recibir más mensajes, responda a este correo.",
        "legal_basis": (
            "Base legal: interés legítimo (GDPR art. 6.1.f). Responda para oponerse."
        ),
    },
    "nl": {
        "optout": "Wilt u zich uitschrijven, antwoord dan op dit bericht.",
        "legal_basis": "Rechtsgrond: gerechtvaardigd belang (GDPR art. 6.1.f).",
    },
    "it": {
        "optout": "Per non ricevere altri messaggi, risponda con «cancella iscrizione».",
        "legal_basis": (
            "Base giuridica: interesse legittimo / legitimate interest (GDPR art. 6.1.f)."
        ),
    },
    "ar": {
        "optout": "إذا لم يكن هذا مناسبًا، يرجى الرد بكلمة إلغاء الاشتراك ولن أتابع.",
        "legal_basis": "",
    },
}


def build_footer(config: TenantConfig, region: str, language: str = "en") -> str:
    """
    The compliance block appended to every email body.

    Built from the REGION'S OWN profile rather than a fixed template, so a
    tenant operating in six regions gets six correct footers without
    maintaining six drafts:

      * sender identification (required everywhere in this file)
      * physical postal address (US CAN-SPAM, Canada CASL)
      * legal-basis statement (EU)
      * an opt-out line that actually contains a phrase the profile matches

    That last point is the subtle one: a tenant can write any opt-out wording
    they like in `signature_optout_line`, but if it does not contain a phrase
    from the profile, N5.5 would block every send. Rather than fail the batch,
    the profile-matching line is appended alongside it.
    """
    profile = config.compliance_profile(region)
    identity = config.sending_identity
    base = (language or "en").lower().split("-")[0]
    phrases = FOOTER_PHRASES.get(base, FOOTER_PHRASES["en"])

    lines: list[str] = []

    if profile.get("requires_sender_identification"):
        who = " ".join(x for x in (
            identity.get("from_name", ""), "-", identity.get("company_name", "")
        ) if x).strip(" -")
        if identity.get("website"):
            who = f"{who} | {identity['website']}"
        lines.append(who)

    if profile.get("requires_physical_address"):
        address = str(identity.get("physical_address", "")).strip()
        if address:
            lines.append(address)
        else:
            log.warning(
                "region %s requires a physical postal address but tenant %s has "
                "sending_identity.physical_address empty; the send will be blocked "
                "at N5.5",
                region, config.tenant_id,
            )

    if profile.get("requires_legal_basis_line") and phrases.get("legal_basis"):
        lines.append(phrases["legal_basis"])

    if profile.get("requires_optout_line"):
        tenant_line = str(identity.get("signature_optout_line", "")).strip()
        accepted = [str(p).lower() for p in (profile.get("optout_phrases") or [])]
        localised = base != "en" and base in FOOTER_PHRASES
        if localised:
            # An English opt-out line under French body copy reads as a mistake
            # and undermines the whole message, so a localised draft always uses
            # the localised line rather than the tenant's English one.
            lines.append(phrases["optout"])
        elif tenant_line and any(p in tenant_line.lower() for p in accepted):
            lines.append(tenant_line)
        else:
            if tenant_line:
                log.debug(
                    "tenant %s's opt-out line does not contain a phrase %s accepts; "
                    "appending the profile-matching line as well",
                    config.tenant_id, region,
                )
                lines.append(tenant_line)
            lines.append(phrases["optout"])

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n4_personalization")
def n4_personalization(state: LeadState, *, tenant_config: TenantConfig | None = None) -> dict:
    """
    Draft this touch's message(s) for the lead's channel.

    Loops up to `personalization.max_self_check_attempts` times. Each attempt
    must produce a draft that (a) demonstrably references one of the lead's
    signals, (b) fits the LinkedIn character limit, and (c) contains none of the
    tenant's forbidden phrases. Failing all attempts flags the lead for manual
    drafting rather than sending generic copy.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])
    channel = state.get("channel", "email")
    step = max(1, int(state.get("sequence_step", 0)) + 1)
    signals = [str(s) for s in (state.get("signals") or [])]
    max_attempts = int(config.personalization.get("max_self_check_attempts", 2))
    forbidden = list(config.personalization.get("forbidden_phrases", []))

    email_draft: dict[str, str] = {}
    linkedin_draft: dict[str, str] = {}
    matched_signal = ""
    failures: list[str] = []

    # Resolved once, not per attempt. The self-check can re-prompt twice and
    # the shorten-note path a third time; rebuilding the adapter each time
    # would re-read the config and re-probe a local model for every draft.
    writer = llm_for(config, tenant_id=state["tenant_id"])

    for attempt in range(1, max_attempts + 1):
        attempt_failures: list[str] = []
        checked_text: list[str] = []
        claimed = ""

        if wants_email(channel):
            email_draft, claimed_email = draft_email(state, config, step, writer)
            checked_text.append(email_draft.get("body", ""))
            claimed = claimed or claimed_email
            hits = _forbidden_hits(
                email_draft.get("subject", "") + " " + email_draft.get("body", ""), forbidden
            )
            if hits:
                attempt_failures.append(f"used forbidden phrase(s): {hits}")

        if wants_linkedin(channel):
            linkedin_draft, claimed_li = draft_linkedin(state, config, step, writer)
            note = shorten_note(
                linkedin_draft.get("connection_note", ""), state, config, writer
            )
            linkedin_draft["connection_note"] = note
            checked_text.append(note + " " + linkedin_draft.get("followup_dm", ""))
            claimed = claimed or claimed_li
            if len(note) > MAX_LINKEDIN_CONNECTION_NOTE_CHARS:
                attempt_failures.append(
                    f"connection note is {len(note)} chars after shortening "
                    f"(limit {MAX_LINKEDIN_CONNECTION_NOTE_CHARS})"
                )
            hits = _forbidden_hits(note + " " + linkedin_draft.get("followup_dm", ""), forbidden)
            if hits:
                attempt_failures.append(f"used forbidden phrase(s): {hits}")

        ok, matched = verify_references_signal(
            " ".join(checked_text), signals, claimed=claimed
        )
        if not ok:
            attempt_failures.append(
                "draft does not reference any specific signal from enrichment"
            )
        else:
            matched_signal = matched

        if not attempt_failures:
            break

        failures.append(f"attempt {attempt}: " + "; ".join(attempt_failures))
        log.warning(
            "self-check failed tenant=%s lead=%s attempt=%d: %s",
            state.get("tenant_id"), state.get("lead_id"), attempt,
            "; ".join(attempt_failures),
        )
    else:
        # Every attempt failed -- do not pass a generic message forward.
        return {
            "needs_manual_review": True,
            "manual_review_reason": (
                "personalization self-check failed "
                f"{max_attempts} time(s): " + " | ".join(failures)
            ),
            "next_action": "MANUAL: draft this message by hand - no specific signal to cite",
            "approval_status": "pending",
        }

    # -- localisation, then the compliance footer -------------------------- #
    identity = config.sending_identity
    language = state.get("language") or config.language_for(state["region"])
    translated = False
    fields: dict[str, str] = {}
    if email_draft:
        fields["email_subject"] = email_draft.get("subject", "")
        fields["email_body"] = email_draft.get("body", "")
    if linkedin_draft:
        fields["linkedin_note"] = linkedin_draft.get("connection_note", "")
        fields["linkedin_dm"] = linkedin_draft.get("followup_dm", "")

    if fields and not translation.is_english(language):
        localised, translated, provider = translation.translate_fields(
            fields, language,
            provider=translation_for(config, tenant_id=state["tenant_id"]),
        )
        if translated:
            log.info(
                "localised draft to %s via %s (tenant=%s lead=%s)",
                language, provider, state.get("tenant_id"), state.get("lead_id"),
            )
            if email_draft:
                email_draft["subject"] = localised.get("email_subject", email_draft["subject"])
                email_draft["body"] = localised.get("email_body", email_draft["body"])
            if linkedin_draft:
                note = localised.get("linkedin_note", linkedin_draft["connection_note"])
                # Translation can push a note back over the limit.
                linkedin_draft["connection_note"] = shorten_note(
                    note, state, config, writer
                )
                linkedin_draft["followup_dm"] = localised.get(
                    "linkedin_dm", linkedin_draft.get("followup_dm", "")
                )

    # The footer is appended AFTER translation and is not itself translated by
    # the model. N5.5 matches exact phrases from the region's compliance
    # profile, and a translator paraphrasing "legitimate interest" or mangling
    # a postal address would fail that check on an otherwise sound draft. The
    # localised variants come from FOOTER_PHRASES instead, verbatim.
    if email_draft:
        email_draft["body"] = (
            email_draft["body"].rstrip() + "\n\n" + build_footer(config, state["region"], language)
        )

    draft: DraftMessage = {
        "signal_referenced": matched_signal,
        "language": language,
        "translated": translated,
    }
    if email_draft:
        draft["email"] = email_draft   # type: ignore[typeddict-item]
    if linkedin_draft:
        draft["linkedin"] = linkedin_draft   # type: ignore[typeddict-item]

    log.info(
        "drafted tenant=%s lead=%s company=%r channel=%s step=%d signal=%r",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        channel, step, matched_signal[:60],
    )

    return {
        "draft_message": draft,
        "approval_status": "pending",
        "language": language,
        "next_action": f"awaiting approval for touch {step} ({channel})",
    }
