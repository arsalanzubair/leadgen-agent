"""
llm.py -- chat model wrapper with provider fallback.

Six real providers, and one that needs no account:

    groq  gemini  openai  anthropic  deepseek  ollama        mock

All are exposed behind one interface so nodes never branch on provider. Which
one a workspace uses is its own choice (`src/providers/resolve.py`); what is
here is how to reach each of them and what to do when one will not answer.

The DEFAULT chain is free-first -- Groq, then Gemini, then a model on the
user's own machine -- because a fresh install has to work without a card on
file. The metered ones are never reached by that default: they run when a
workspace picks one, which is the point of them being here.

`mock` is selected automatically when no key is configured at all. It is not a
stub for tests to skip past -- it is what makes `--dry-run` work on a fresh
clone with an empty .env, which Section 9 requires as the default way to test a
change. Its responses are deterministic and derived from the actual lead data,
so a dry run exercises the real prompts, the real JSON parsing, the real
self-check loop and the real routing logic.

Env: LLM_PROVIDER,
     GROQ_API_KEY, GROQ_MODEL,
     GOOGLE_API_KEY, GEMINI_MODEL,
     OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL,
     ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_BASE_URL,
     DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL,
     OLLAMA_BASE_URL, OLLAMA_MODEL
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

from src.reliability import log, retry_once
from src.settings import env

#: Tried in this order when the configured provider is unavailable or fails.
#:
#: Free tiers and local hardware only. A metered provider is never reached by
#: falling through to it -- a workspace that has not chosen to spend money must
#: not start spending it because a free tier was rate-limited.
FALLBACK_ORDER: tuple[str, ...] = ("groq", "gemini", "ollama", "mock")

#: Everything `complete()` knows how to talk to, including the ones that only
#: run when a workspace picks them. `resolve_provider_chain` filters against
#: this rather than against FALLBACK_ORDER -- otherwise choosing Claude would
#: silently drop it and run Groq.
KNOWN_PROVIDERS: tuple[str, ...] = (
    "groq", "gemini", "openai", "anthropic", "deepseek", "ollama", "mock",
)

#: The metered ones. Data rather than a remembered rule: these reach a chain
#: only when somebody names them.
PAID_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "deepseek"})


class LLMUnavailable(RuntimeError):
    """No provider could answer. N3/N4/N7 route the lead to manual review."""


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str = ""
    raw: Any = field(default=None, repr=False)


# --------------------------------------------------------------------------- #
# Provider availability
# --------------------------------------------------------------------------- #

#: provider -> the environment variable that makes it usable. Ollama and mock
#: are absent because neither needs one.
KEY_VARS: dict[str, str] = {
    "groq": "GROQ_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def provider_available(name: str) -> bool:
    key_var = KEY_VARS.get(name)
    if key_var:
        return bool(env(key_var))
    if name == "ollama":
        return _ollama_reachable(env("OLLAMA_BASE_URL", "http://localhost:11434"))
    return name == "mock"


@lru_cache(maxsize=4)
def _ollama_reachable(base_url: str) -> bool:
    """
    Cheap TCP probe -- a local Ollama that is not running must fall through to
    the next provider rather than block a batch on a connection timeout.

    Cached for the life of the process: this is called once per LLM call, and
    paying a 0.75s connect timeout per lead per node would dominate a dry run.
    An Ollama started mid-batch is therefore not picked up until the next run,
    which is the right trade for a fallback provider.
    """
    import socket
    import urllib.parse

    url = urllib.parse.urlsplit(base_url)
    try:
        with socket.create_connection((url.hostname or "localhost", url.port or 11434), 0.75):
            return True
    except OSError:
        return False


def configured_provider() -> str:
    return env("LLM_PROVIDER", "groq").lower().strip()


def resolve_provider_chain(preferred: str = "") -> list[str]:
    """
    The providers to try, in order: the preferred one first, then the rest of
    the fallback order. `mock` is always last so it can never shadow a real
    provider the operator configured.
    """
    preferred = (preferred or configured_provider()).lower().strip()
    chain = [preferred] + [p for p in FALLBACK_ORDER if p != preferred]
    return [p for p in chain if p in KNOWN_PROVIDERS]


# --------------------------------------------------------------------------- #
# Provider clients
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=8)
def _build_client(provider: str, temperature_x100: int) -> Any:
    """Cached LangChain chat model. Temperature is an int key so it hashes."""
    temperature = temperature_x100 / 100.0
    if provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=env("GROQ_MODEL", "llama-3.3-70b-versatile"),
            temperature=temperature,
            api_key=env("GROQ_API_KEY"),
            max_retries=0,       # retry policy is ours (retry_once), not theirs
        )
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=env("GEMINI_MODEL", "gemini-2.0-flash"),
            temperature=temperature,
            google_api_key=env("GOOGLE_API_KEY"),
            max_retries=0,
        )
    if provider == "openai":
        from src.integrations.hosted_chat import OpenAICompatibleChat

        return OpenAICompatibleChat(
            api_key=env("OPENAI_API_KEY"),
            model=model_name("openai"),
            base_url=env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            temperature=temperature,
            provider="OpenAI",
        )
    if provider == "deepseek":
        from src.integrations.hosted_chat import OpenAICompatibleChat

        # DeepSeek implements the OpenAI chat-completions shape deliberately,
        # so it is the same client pointed somewhere else.
        return OpenAICompatibleChat(
            api_key=env("DEEPSEEK_API_KEY"),
            model=model_name("deepseek"),
            base_url=env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            temperature=temperature,
            provider="DeepSeek",
        )
    if provider == "anthropic":
        from src.integrations.hosted_chat import AnthropicChat

        return AnthropicChat(
            api_key=env("ANTHROPIC_API_KEY"),
            model=model_name("anthropic"),
            base_url=env("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            temperature=temperature,
        )
    if provider == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=env("OLLAMA_MODEL", "llama3.1:8b"),
            temperature=temperature,
            base_url=env("OLLAMA_BASE_URL", "http://localhost:11434"),
        )
    raise LLMUnavailable(f"no client for provider {provider!r}")


def model_name(provider: str) -> str:
    return {
        "groq": env("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "gemini": env("GEMINI_MODEL", "gemini-2.0-flash"),
        "openai": env("OPENAI_MODEL", "gpt-4o-mini"),
        "anthropic": env("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "deepseek": env("DEEPSEEK_MODEL", "deepseek-chat"),
        "ollama": env("OLLAMA_MODEL", "llama3.1:8b"),
        "mock": "deterministic-mock",
    }.get(provider, provider)


# --------------------------------------------------------------------------- #
# The mock provider
# --------------------------------------------------------------------------- #

#: Node prompts register a deterministic responder here. Keyed by the `task`
#: string each caller passes to `complete()`.
_MOCK_HANDLERS: dict[str, Callable[[str, dict], str]] = {}


def register_mock(task: str, handler: Callable[[str, dict], str]) -> None:
    """Register the deterministic offline responder for one task."""
    _MOCK_HANDLERS[task] = handler


def _mock_complete(prompt: str, task: str, context: dict) -> str:
    handler = _MOCK_HANDLERS.get(task)
    if handler is None:
        raise LLMUnavailable(
            f"no mock handler registered for task {task!r}. A dry run must be "
            "able to exercise every LLM-backed node offline; register one with "
            "llm.register_mock()."
        )
    return handler(prompt, context)


# --------------------------------------------------------------------------- #
# The public interface
# --------------------------------------------------------------------------- #

def complete(
    prompt: str,
    *,
    task: str,
    system: str = "",
    temperature: float = 0.4,
    context: dict | None = None,
    provider: str = "",
    allow_mock: bool = True,
    chain: tuple[str, ...] = (),
) -> LLMResponse:
    """
    Send one prompt and return the text, walking the provider chain on failure.

    Each provider gets a retry-once (Section 8) before the chain moves on, so a
    transient Groq 429 costs one retry rather than a whole lead. Exhausting the
    chain raises LLMUnavailable, which @node converts into a
    needs_manual_review routing decision rather than a crash.

    `chain` overrides the order entirely, and is how the capability layer makes
    a tenant's own primary/fallback choice real: without it, a tenant that
    picked Gemini with Ollama behind it would still get FALLBACK_ORDER's
    Gemini-then-Groq. Passing nothing keeps the historical behaviour, which is
    what every existing caller and test relies on.
    """
    context = context or {}
    errors: list[str] = []
    order = tuple(chain) if chain else tuple(resolve_provider_chain(provider))

    for name in order:
        if name == "mock":
            if not allow_mock:
                continue
            try:
                return LLMResponse(
                    text=_mock_complete(prompt, task, context),
                    provider="mock", model=model_name("mock"),
                )
            except LLMUnavailable as exc:
                errors.append(f"mock: {exc}")
                continue

        if not provider_available(name):
            continue

        try:
            client = _build_client(name, int(round(temperature * 100)))
            messages: list[tuple[str, str]] = []
            if system:
                messages.append(("system", system))
            messages.append(("human", prompt))
            result = retry_once(client.invoke, messages, _label=f"llm:{name}:{task}")
            text = getattr(result, "content", "")
            if isinstance(text, list):   # some providers return content blocks
                text = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in text
                )
            if not str(text).strip():
                raise RuntimeError("empty response")
            return LLMResponse(text=str(text), provider=name, model=model_name(name), raw=result)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            log.warning("llm provider %s failed for task=%s: %s", name, task, exc)

    raise LLMUnavailable(
        f"every LLM provider failed for task={task!r}: " + "; ".join(errors)
    )


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict:
    """
    Pull a JSON object out of a model response.

    Models wrap JSON in prose or code fences no matter how firmly the prompt
    forbids it, so this tries the fenced block, then the outermost brace pair,
    before giving up. Raises ValueError, which callers convert into a retry.
    """
    text = (text or "").strip()
    candidates: list[str] = []

    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.append(text)

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON object found in model response: {text[:300]!r}")


def complete_json(
    prompt: str,
    *,
    task: str,
    system: str = "",
    temperature: float = 0.2,
    context: dict | None = None,
    required_keys: tuple[str, ...] = (),
    provider: str = "",
    chain: tuple[str, ...] = (),
) -> tuple[dict, LLMResponse]:
    """
    Ask for JSON and parse it, retrying once with a blunter instruction if the
    first response is unparseable or missing a required key.
    """
    json_system = (
        (system + "\n\n" if system else "")
        + "Respond with a single valid JSON object and nothing else. "
          "No prose, no markdown code fences, no explanation."
    )

    response = complete(
        prompt, task=task, system=json_system, temperature=temperature,
        context=context, provider=provider, chain=chain,
    )
    try:
        parsed = extract_json(response.text)
        missing = [key for key in required_keys if key not in parsed]
        if missing:
            raise ValueError(f"response is missing required keys {missing}")
        return parsed, response
    except ValueError as exc:
        log.warning("llm json parse failed for task=%s (%s); retrying once", task, exc)

    response = complete(
        prompt + f"\n\nYour previous reply could not be parsed. Return ONLY a JSON "
                 f"object with these keys: {list(required_keys)}.",
        task=task, system=json_system, temperature=0.0, context=context,
        provider=provider, chain=chain,
    )
    parsed = extract_json(response.text)
    missing = [key for key in required_keys if key not in parsed]
    if missing:
        raise ValueError(f"response is still missing required keys {missing}")
    return parsed, response


def active_provider() -> str:
    """The provider that would actually serve a call right now. For CLI banners."""
    for name in resolve_provider_chain():
        if name == "mock" or provider_available(name):
            return name
    return "mock"
