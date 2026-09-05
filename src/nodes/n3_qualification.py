"""
N3 -- Qualification.

In:  LeadState with signals, plus the matched niche's ICP config
Out: fit_score (0-100), fit_reason

LLM-scored (Groq primary; Gemini or Ollama fallback; deterministic mock when no
key is configured, so a dry run scores offline). Retries once; a second failure
routes THAT LEAD to needs_manual_review rather than blocking the batch. Leads
below the tenant threshold are archived by the conditional edge after this
node -- N3 itself only scores and explains.

The score is deliberately explained in `fit_reason` in one sentence: an
operator reviewing the approval queue needs to know why a lead scored 78 in
less time than it takes to re-read the ICP.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.integrations import llm
from src.providers import llm_for
from src.nodes.n0_config_load import TenantConfig, load_tenant_config
from src.reliability import log, node
from src.state import LeadState

SYSTEM_PROMPT = (
    "You are a lead qualification analyst for a B2B agency. You score how well "
    "a discovered company matches a specific ideal-customer profile. You are "
    "sceptical: a company with no observable buying signal scores low no matter "
    "how large or well known it is. You never invent facts about a company that "
    "are not in the evidence you are given."
)

PROMPT_TEMPLATE = """\
Score how well this company matches the ideal customer profile.

# Ideal customer profile
{icp_description}

Signals that indicate a GOOD fit:
{good_signals}

Things that DISQUALIFY a company:
{disqualifiers}

Typical size: {size_hint}

# The company
Name: {company_name}
Industry: {industry}
Location: {location} (region: {region})
Website: {website}
LinkedIn: {linkedin_url}
Known contact: {contact_name} <{contact_email}>

# Evidence gathered during enrichment
{signals}

# How to score
- 80-100: several strong, specific signals from the good-fit list; clearly in profile.
- 60-79:  in profile with at least one concrete, specific signal.
- 40-59:  plausibly in profile but the evidence is thin or generic.
- 20-39:  weak match, or the only evidence is generic.
- 0-19:   out of profile, or a disqualifier is present.

A disqualifier caps the score at 19 regardless of any other signal.
No evidence at all caps the score at 35 -- absence of evidence is not a fit.

Return JSON only:
{{"fit_score": <integer 0-100>, "fit_reason": "<one sentence, max 30 words, \
citing the specific evidence that drove the score>"}}
"""


def _format_list(items: list[Any] | None, empty: str = "(none provided)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def build_prompt(state: LeadState, niche: dict[str, Any]) -> str:
    icp = niche.get("icp") or {}
    return PROMPT_TEMPLATE.format(
        icp_description=icp.get("description", "(not specified)"),
        good_signals=_format_list(icp.get("good_signals")),
        disqualifiers=_format_list(icp.get("disqualifiers"), "(none specified)"),
        size_hint=icp.get("size_hint", "(not specified)"),
        company_name=state.get("company_name", ""),
        industry=state.get("industry", "") or "(unknown)",
        location=state.get("location", "") or "(unknown)",
        region=state.get("region", ""),
        website=state.get("website", "") or "(none found)",
        linkedin_url=state.get("linkedin_url", "") or "(none found)",
        contact_name=state.get("contact_name", "") or "(unknown)",
        contact_email=state.get("contact_email", "") or "(none found)",
        signals=_format_list(state.get("signals"), "(no signals found during enrichment)"),
    )


# --------------------------------------------------------------------------- #
# Deterministic offline scorer (the `mock` provider)
# --------------------------------------------------------------------------- #

def _keywords(phrase: str) -> set[str]:
    return {word for word in re.split(r"\W+", phrase.lower()) if len(word) > 3}


def score_offline(state: LeadState, niche: dict[str, Any]) -> tuple[int, str]:
    """
    Rule-based scoring used when no LLM provider is configured.

    This is not a random number: it applies the same rubric the prompt
    describes -- overlap with the ICP's good_signals, disqualifier veto, and a
    hard cap when there is no evidence -- so a dry run produces plausible,
    explainable scores and genuinely exercises the threshold routing.
    """
    icp = niche.get("icp") or {}
    signals = [str(s) for s in (state.get("signals") or [])]
    blob = " ".join(signals).lower()

    for disqualifier in icp.get("disqualifiers") or []:
        if _keywords(str(disqualifier)) and _keywords(str(disqualifier)) <= _keywords(blob):
            return 10, f"Disqualified: evidence matches '{disqualifier}'."
    if "DISQUALIFIER found" in " ".join(signals):
        return 10, "Disqualified: a disqualifier phrase was found on their site."

    if not signals:
        return 20, "No signals found during enrichment; nothing to qualify on."

    good = [str(s) for s in (icp.get("good_signals") or [])]
    matched = [
        signal for signal in good
        if _keywords(signal) and len(_keywords(signal) & _keywords(blob)) >= max(
            1, len(_keywords(signal)) // 2
        )
    ]

    score = 35 + 15 * len(matched) + 3 * min(len(signals), 4)
    if state.get("contact_email"):
        score += 5
    if state.get("contact_name"):
        score += 3
    score = max(0, min(100, score))

    if matched:
        reason = (
            f"Matches {len(matched)} ICP signal(s), notably '{matched[0]}', "
            f"across {len(signals)} pieces of evidence."
        )
    else:
        reason = (
            f"{len(signals)} signal(s) found but none map cleanly to the ICP's "
            "good-fit list."
        )
    return score, reason[:200]


def _mock_handler(prompt: str, context: dict) -> str:
    state: LeadState = context.get("state") or {}
    niche: dict[str, Any] = context.get("niche") or {}
    score, reason = score_offline(state, niche)
    return json.dumps({"fit_score": score, "fit_reason": reason})


llm.register_mock("qualification", _mock_handler)


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

def _coerce_score(value: Any) -> int:
    """Models return 85, '85', 85.0 or '85/100'. Accept all, clamp the result."""
    if isinstance(value, bool):
        raise ValueError("fit_score must be a number, got a boolean")
    if isinstance(value, (int, float)):
        return max(0, min(100, int(round(value))))
    match = re.search(r"\d+", str(value))
    if not match:
        raise ValueError(f"fit_score {value!r} contains no number")
    return max(0, min(100, int(match.group())))


@node("n3_qualification")
def n3_qualification(state: LeadState, *, tenant_config: TenantConfig | None = None) -> dict:
    """
    Score one lead against its niche's ICP.

    `complete_json` already retries once internally on a parse failure, and
    `retry_once` inside the LLM wrapper retries each provider once on a
    transport failure. If the whole chain is still exhausted, the exception
    reaches @node, which flags the lead needs_manual_review -- the batch
    continues.
    """
    config = tenant_config or load_tenant_config(state["tenant_id"])
    niche = config.niche(state["niche_id"])

    writer = llm_for(config, tenant_id=state["tenant_id"])
    parsed, response = writer.complete_json(
        build_prompt(state, niche),
        task="qualification",
        system=SYSTEM_PROMPT,
        temperature=0.1,
        context={"state": state, "niche": niche},
        required_keys=("fit_score", "fit_reason"),
    )

    score = _coerce_score(parsed["fit_score"])
    reason = str(parsed["fit_reason"]).strip()[:400]
    if not reason:
        reason = "Model returned no reason."

    log.info(
        "qualified tenant=%s lead=%s company=%r score=%d threshold=%d provider=%s",
        state.get("tenant_id"), state.get("lead_id"), state.get("company_name"),
        score, config.fit_score_threshold, response.provider,
    )

    updates: dict[str, Any] = {"fit_score": score, "fit_reason": reason}
    if score < config.fit_score_threshold:
        updates["archived"] = True
        updates["archive_reason"] = "below_threshold"
        updates["next_action"] = (
            f"archived: fit {score} is below the tenant threshold "
            f"{config.fit_score_threshold}"
        )
    return updates


# --------------------------------------------------------------------------- #
# Conditional edge helper (used by graph.py)
# --------------------------------------------------------------------------- #

def route_after_qualification(state: LeadState) -> str:
    """
    Section 5: the conditional edge after N3 compares fit_score to the tenant
    threshold. A lead flagged for manual review or already archived also leaves
    the automated path here.
    """
    if state.get("needs_manual_review"):
        return "manual_review"
    if state.get("archived") or state.get("unreachable"):
        return "archive"
    config = load_tenant_config(state["tenant_id"])
    if int(state.get("fit_score", 0)) < config.fit_score_threshold:
        return "archive"
    return "channel_selection"
