"""
translation.py -- localisation.

DeepL API Free (500k characters/month, enforced by a durable counter), falling
back to prompting the primary LLM to translate when DeepL is unavailable, has
no key, or the monthly budget is spent.

Cold outreach copy is not generic text: an idiomatic-but-wrong translation of a
call to action costs a reply. So the LLM fallback prompt is explicit about
register and about NOT translating proper nouns -- a company called "Bright
Smile Dental" stays "Bright Smile Dental" in French.

Env: DEEPL_API_KEY, DEEPL_MONTHLY_CHAR_CAP
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.counters import QuotaExceeded, deepl_counter
from src.integrations import llm
from src.reliability import log, retry_once
from src.settings import env

#: BCP-47-ish tag -> (DeepL target code, human-readable name for the LLM prompt).
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("EN-GB", "English"),
    "en-us": ("EN-US", "American English"),
    "en-gb": ("EN-GB", "British English"),
    "fr": ("FR", "French"),
    "de": ("DE", "German"),
    "es": ("ES", "Spanish"),
    "it": ("IT", "Italian"),
    "nl": ("NL", "Dutch"),
    "pt": ("PT-PT", "Portuguese"),
    "pl": ("PL", "Polish"),
    "sv": ("SV", "Swedish"),
    "ar": ("", "Arabic"),        # DeepL has no Arabic target -> LLM fallback
}


@dataclass
class Translation:
    text: str
    language: str
    provider: str          # deepl | llm | none
    translated: bool


def language_name(language: str) -> str:
    return LANGUAGES.get((language or "en").lower(), ("", language))[1]


def deepl_target(language: str) -> str:
    return LANGUAGES.get((language or "").lower(), ("", ""))[0]


def is_english(language: str) -> bool:
    return (language or "en").lower().split("-")[0] == "en"


def has_deepl_key() -> bool:
    return bool(env("DEEPL_API_KEY"))


# --------------------------------------------------------------------------- #
# DeepL
# --------------------------------------------------------------------------- #

def _translate_deepl(text: str, target: str) -> str:
    import deepl

    translator = deepl.Translator(env("DEEPL_API_KEY"))
    result = translator.translate_text(
        text,
        target_lang=target,
        # Cold outreach to a stranger is formal in French, German and Dutch.
        formality="prefer_more",
        preserve_formatting=True,
    )
    return result.text if hasattr(result, "text") else str(result)


# --------------------------------------------------------------------------- #
# LLM fallback
# --------------------------------------------------------------------------- #

LLM_SYSTEM = (
    "You are a professional translator specialising in business correspondence. "
    "You translate cold outreach emails so they read as if written by a native "
    "speaker, not translated. You never add, remove or soften content."
)

LLM_PROMPT = """\
Translate the text below into {language}.

Rules:
- Keep the same meaning, structure and line breaks exactly.
- Use the formal register appropriate for writing to a stranger in business.
- Do NOT translate proper nouns: company names, product names, people's names.
- Do NOT translate anything inside [square brackets].
- Return ONLY the translated text. No preamble, no notes, no quotes around it.

Text:
{text}
"""


def _translate_llm(text: str, language: str) -> str:
    response = llm.complete(
        LLM_PROMPT.format(language=language_name(language), text=text),
        task="translation",
        system=LLM_SYSTEM,
        temperature=0.1,
        context={"text": text, "language": language},
    )
    return response.text.strip()


def _mock_translation(prompt: str, context: dict) -> str:
    """
    Offline stand-in so dry runs exercise the localisation path.

    It deliberately does NOT fake a translation -- inventing French would make a
    dry run look correct when the real path is broken. It tags the text with the
    target language so the operator can see localisation was applied and to
    which language.
    """
    language = context.get("language", "??")
    text = context.get("text", "")
    return f"[{language.upper()} translation pending - dry run]\n{text}"


llm.register_mock("translation", _mock_translation)


# --------------------------------------------------------------------------- #
# Public interface
# --------------------------------------------------------------------------- #

def nothing_to_do(text: str, language: str) -> Translation | None:
    """
    The cases where no provider should be asked at all.

    Empty text, or a target language the outreach is already written in.
    Returned as a real `Translation` with `translated=False` rather than as an
    error, because neither case is a failure to report to anyone.
    """
    if not (text or "").strip():
        return Translation(text="", language=language, provider="none", translated=False)
    if is_english(language):
        return Translation(
            text=text.strip(), language=language, provider="none", translated=False
        )
    return None


def deepl_translation(text: str, language: str) -> Translation | None:
    """
    The DeepL branch, on its own.

    `None` means "I could not do this, try the next provider" -- no key, no
    supported target for this language, quota spent, or a failing call. The
    character budget is checked BEFORE the call and consumed after, so a
    request that would overrun the free 500k allowance is never sent.
    """
    text = (text or "").strip()
    target = deepl_target(language)
    if not target:
        log.info("DeepL has no target for %r; another provider will have to do it", language)
        return None
    if not has_deepl_key():
        return None

    counter = deepl_counter()
    try:
        counter.check(len(text))
        translated = retry_once(_translate_deepl, text, target, _label="deepl")
        counter.consume(len(text))
        log.info(
            "translated %d chars to %s via DeepL (%s)",
            len(text), target, counter.status(),
        )
        return Translation(
            text=translated, language=language, provider="deepl", translated=True
        )
    except QuotaExceeded as exc:
        log.warning("%s; another provider will have to do it", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("DeepL failed (%s); another provider will have to do it", exc)
        return None


def llm_translation(text: str, language: str) -> Translation | None:
    """
    The model branch, on its own. Uses whatever the tenant writes with.

    `None` on any failure. Keeping the English text is the correct
    degradation, and the caller records that so a human reviewing the draft can
    see localisation did not run.
    """
    text = (text or "").strip()
    try:
        translated = _translate_llm(text, language)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "LLM translation to %s failed (%s); keeping the English text", language, exc
        )
        return None
    if not translated:
        return None
    return Translation(
        text=translated, language=language, provider="llm", translated=True
    )


def translate(text: str, language: str) -> Translation:
    """
    Translate `text` into `language`, DeepL first and the model behind it.

    Never raises: a failed translation returns the ORIGINAL text with
    `translated=False`. Sending readable English beats sending nothing, and N4
    records the flag so the approval CLI can show the operator that
    localisation did not happen.

    This is the historical entry point and its behaviour is unchanged. The
    capability layer calls the two branch functions above directly, so that a
    workspace which chose its own order gets that order rather than this one.
    """
    settled = nothing_to_do(text, language)
    if settled is not None:
        return settled

    text = text.strip()
    for branch in (deepl_translation, llm_translation):
        result = branch(text, language)
        if result is not None:
            return result

    return Translation(text=text, language=language, provider="none", translated=False)


def translate_fields(
    fields: dict[str, str], language: str, provider: Any | None = None
) -> tuple[dict[str, str], bool, str]:
    """
    Translate several short fields (subject, body, connection note) in one pass.

    Fields are joined with a rare delimiter and sent as ONE request rather than
    three: it saves two thirds of the character budget and, more importantly,
    keeps the register consistent between a subject line and the body it
    belongs to.

    `provider` is the workspace's translation chain. Left as None this uses
    `translate()` and therefore the historical DeepL-then-model order, which
    is what every direct caller and test expects.
    """
    if is_english(language) or not fields:
        return dict(fields), False, "none"

    def _translate_one(text: str) -> Translation:
        """
        One translation, through the workspace's chain or the default order.

        A provider returning None means every link in its chain declined, which
        is the same outcome as an untranslated `Translation` -- normalised here
        so the batching logic below has one shape to reason about.
        """
        if provider is None:
            return translate(text, language)
        result = provider.translate(text, language)
        if result is None:
            return Translation(
                text=text, language=language, provider="none", translated=False
            )
        return Translation(
            text=result.text,
            language=result.language,
            provider=result.provider,
            translated=result.translated,
        )

    delimiter = "\n<<<|FIELD|>>>\n"
    keys = list(fields)
    joined = delimiter.join(fields[key] or "" for key in keys)

    result = _translate_one(joined)
    if not result.translated:
        return dict(fields), False, result.provider

    parts = result.text.split(delimiter.strip())
    if len(parts) != len(keys):
        # The model or DeepL mangled the delimiter. Fall back to per-field
        # translation rather than silently pairing a subject with a body.
        log.warning(
            "batched translation returned %d parts for %d fields; retrying per field",
            len(parts), len(keys),
        )
        out: dict[str, str] = {}
        any_translated = False
        # Named for what it holds -- the NAME of whoever last succeeded -- and
        # deliberately not `provider`, which is the injected chain.
        used_provider = result.provider
        for key in keys:
            single = _translate_one(fields[key] or "")
            out[key] = single.text
            any_translated = any_translated or single.translated
            used_provider = single.provider if single.translated else used_provider
        return out, any_translated, used_provider

    return (
        {key: part.strip() for key, part in zip(keys, parts)},
        True,
        result.provider,
    )
