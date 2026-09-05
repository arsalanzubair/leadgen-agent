"""
llm_adapters.py -- the writing capability, behind one interface.

Wraps `src/integrations/llm.py`, which keeps everything that matters: the
retry-once policy, the JSON re-prompt, the LangChain client cache, the cheap
TCP probe that stops an absent Ollama from stalling a batch, and the
deterministic mock the test suite drives. None of that is reimplemented here.

What this adds is whose choice the provider is. `llm.py` decides by asking the
environment "is there a GROQ_API_KEY?"; the adapter is told, by the tenant's
configuration, and passes an explicit chain down so a workspace that picked
Gemini with Ollama behind it gets exactly that -- not FALLBACK_ORDER's opinion.
"""

from __future__ import annotations

from src.integrations import llm
from src.providers.base import ProviderContext
from src.providers.registry import ProviderSpec
from src.reliability import log


class LLMAdapter:
    """
    One provider from `llm.FALLBACK_ORDER`, with the tenant's fallback behind it.

    The chain is built here rather than inside `llm.py` because it is a
    property of the workspace's configuration, and `llm.py` has no business
    reading tenant YAML.
    """

    def __init__(self, provider_id: str, *, fallback: str = "") -> None:
        self.id = provider_id
        self._fallback = fallback
        self._chain = self._build_chain(provider_id, fallback)

    @staticmethod
    def _build_chain(primary: str, fallback: str) -> tuple[str, ...]:
        """
        The tenant's choices first, then the free providers behind them.

        Two lists, and the difference between them is the whole point:

          KNOWN_PROVIDERS  everything `llm._build_client` can construct. The
                           workspace's own primary and fallback are filtered
                           against this, so choosing Claude gets Claude.
          FALLBACK_ORDER   free tiers and local hardware only. What runs when
                           the chosen provider cannot answer.

        Filtering the chosen ones against FALLBACK_ORDER -- which this used to
        do -- dropped every paid provider silently and ran Groq instead, so a
        workspace that had connected and selected Claude would have been
        writing with something else and had no way to tell.

        The tail is free-only in the other direction too: a workspace on Groq
        does not start spending money because Groq was rate-limited. `mock`
        stays last so it can never shadow a provider somebody paid for.
        """
        wanted = [p for p in (primary, fallback) if p and p in llm.KNOWN_PROVIDERS]
        rest = [p for p in llm.FALLBACK_ORDER if p not in wanted]
        return tuple(wanted + rest)

    @property
    def chain(self) -> tuple[str, ...]:
        return self._chain

    def available(self) -> bool:
        """
        True when any link in the chain can answer.

        Deliberately not "is my primary reachable": a workspace with Gemini
        connected and Groq selected is a workspace that can still run, and
        reporting it as unavailable would block a batch that would have worked.
        """
        return any(
            name == "mock" or llm.provider_available(name) for name in self._chain
        )

    def model_name(self) -> str:
        return llm.model_name(self.active())

    def active(self) -> str:
        """Which link would actually serve the next call. For log lines."""
        for name in self._chain:
            if name == "mock" or llm.provider_available(name):
                return name
        return self._chain[-1] if self._chain else self.id

    def complete(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.4,
        context: dict | None = None,
    ) -> llm.LLMResponse:
        return llm.complete(
            prompt,
            task=task,
            system=system,
            temperature=temperature,
            context=context,
            chain=self._chain,
        )

    def complete_json(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.2,
        context: dict | None = None,
        required_keys: tuple[str, ...] = (),
    ) -> tuple[dict, llm.LLMResponse]:
        return llm.complete_json(
            prompt,
            task=task,
            system=system,
            temperature=temperature,
            context=context,
            required_keys=required_keys,
            chain=self._chain,
        )

    def __repr__(self) -> str:  # pragma: no cover - log readability only
        return f"<LLM {self.id} chain={'>'.join(self._chain)}>"


def build(spec: ProviderSpec, ctx: ProviderContext) -> LLMAdapter:
    """
    Registry factory. `options["fallback"]` is the tenant's second choice.

    A dry run does not force the mock here. The mock is reached the same way
    anything else is -- by being in the chain when nothing above it can answer
    -- which is what makes a dry run with real keys exercise the real model.
    """
    fallback = str(ctx.option("fallback", "") or "")
    adapter = LLMAdapter(spec.id, fallback=fallback)
    log.debug("llm capability -> %r", adapter)
    return adapter
