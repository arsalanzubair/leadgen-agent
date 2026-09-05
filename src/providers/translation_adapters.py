"""
translation_adapters.py -- localising a draft, behind one interface.

Two providers, both wrapping a branch of `integrations.translation`:

  deepl   the metered service, with its 500k-character monthly counter
  llm     the same model the tenant writes their outreach with

`translate()` in that module used to hold the DeepL-then-model order itself.
It still behaves identically for its existing callers; the order it applies is
now one possible ordering rather than the only one, and a workspace that
prefers the model it already pays for -- or has no DeepL account at all -- gets
that instead of a wasted attempt.

`None` from `translate()` here means "I could not, try the next provider". It
is the resolver, not the adapter, that decides there is no next provider and
that the English text goes out with `translated=False` recorded on it.
"""

from __future__ import annotations

from src.integrations import translation
from src.providers.base import ProviderContext, TranslatedText
from src.providers.registry import ProviderSpec


def _as_translated(result: translation.Translation | None) -> TranslatedText | None:
    if result is None:
        return None
    return TranslatedText(
        text=result.text,
        language=result.language,
        provider=result.provider,
        translated=result.translated,
    )


class DeepLTranslation:
    """DeepL, with the free-tier character budget checked before each call."""

    id = "deepl"

    def available(self) -> bool:
        return translation.has_deepl_key()

    def translate(self, text: str, language: str) -> TranslatedText | None:
        settled = translation.nothing_to_do(text, language)
        if settled is not None:
            return _as_translated(settled)
        return _as_translated(translation.deepl_translation(text, language))


class LLMTranslation:
    """
    The tenant's own writing model, doing the translation too.

    Always `available()`: it needs no credential of its own, and whether the
    model behind it can answer is the LLM capability's problem, reported
    through that capability's own chain. Claiming unavailability here would
    hide a working fallback.
    """

    id = "llm"

    def available(self) -> bool:
        return True

    def translate(self, text: str, language: str) -> TranslatedText | None:
        settled = translation.nothing_to_do(text, language)
        if settled is not None:
            return _as_translated(settled)
        return _as_translated(translation.llm_translation(text, language))


class NoTranslation:
    """
    Sends the English text and records that nothing was localised.

    The null adapter, and never silent: `translated=False` reaches the approval
    screen, so a human sees that a French lead is about to get an English
    email and can decide whether that is acceptable.
    """

    id = "none"

    def available(self) -> bool:
        return True

    def translate(self, text: str, language: str) -> TranslatedText | None:
        return TranslatedText(
            text=(text or "").strip(),
            language=language,
            provider="none",
            translated=False,
        )


_ADAPTERS = {
    "deepl": DeepLTranslation,
    "llm": LLMTranslation,
    "none": NoTranslation,
}


def build(spec: ProviderSpec, ctx: ProviderContext):
    """Registry factory."""
    adapter = _ADAPTERS.get(spec.id, NoTranslation)
    return adapter()
