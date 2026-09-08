"""
redact.py -- take credentials out of anything on its way to a log or a screen.

A failing HTTP call usually quotes the request back, and for some providers the
key travels in the query string: Gemini's REST calls carry `?key=...`. So an
error message passed through unchanged can put the user's own API key into a
log file, a terminal, and the browser. That has to be impossible rather than
unlikely, so redaction lives in one function and every error that leaves the
LLM layer goes through it.

Deliberately blunt. Losing a few characters of a diagnostic is a small cost;
printing a live credential is not.
"""

from __future__ import annotations

import re

#: Ordered most specific first, so a labelled value keeps its label.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=..., api_key=..., token=..., access-token=...
    re.compile(r"""(?i)\b(key|api[-_]?key|access[-_]?token|token|secret)=[^&\s"'<>]+"""),
    # Provider-shaped keys, recognisable by their prefix.
    re.compile(r"\bAIza[0-9A-Za-z_-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b(?:gsk|xai|hf|pat)_[A-Za-z0-9_-]{10,}"),
    # A bearer token in a quoted header dump.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{10,}"),
    # Anything else long enough to be a credential rather than a word. Model
    # names ("gemini-2.0-flash") and node names are far shorter than this.
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
)


def _replace(match: re.Match[str]) -> str:
    text = match.group(0)
    if "=" in text:
        return text.split("=", 1)[0] + "=<redacted>"
    if text[:6].lower() == "bearer":
        return "Bearer <redacted>"
    return "<redacted>"


def scrub(text: str) -> str:
    """Replace anything credential-shaped with a marker, keeping the rest."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(_replace, text)
    return text
