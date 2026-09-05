"""
hosted_chat.py -- OpenAI, Anthropic and DeepSeek, over their own REST APIs.

Written against `requests` rather than each vendor's SDK, for the same reason
Brevo is: three more SDKs would be three more dependency trees, three more
release cadences and three more ways for a transitive pin to break an install,
in exchange for saving about forty lines of JSON handling.

Each client exposes the one method the rest of the system already expects:

    client.invoke(messages) -> object with `.content`

which is LangChain's chat-model shape. That is deliberate -- `llm.complete()`
walks a provider chain and calls `.invoke()` on whatever it gets, so Groq
(LangChain), Gemini (LangChain) and these three (plain HTTP) are the same thing
to every caller, and none of the nodes learn a fourth way to ask a question.

Two wire formats between them:

  * OpenAI and DeepSeek speak chat-completions. DeepSeek is deliberately
    OpenAI-compatible, so it is the same client with a different base URL.
  * Anthropic speaks its Messages API: `x-api-key` rather than a bearer token,
    a required `anthropic-version` header, the system prompt as a TOP-LEVEL
    field rather than a message with role "system", and a content list of
    typed blocks in the response.

Keys are read by the caller and passed in. Nothing here reads the environment,
and nothing here logs a key -- errors carry the status code and the provider's
own message, which is what makes a wrong key say "incorrect API key" instead of
"something went wrong".

Env (read by llm.py, listed here so the defaults are findable):
  OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL
  ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_BASE_URL
  DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import requests

#: Anthropic pins its API by date. Sent on every request; the API rejects a
#: call without it rather than guessing.
ANTHROPIC_VERSION = "2023-06-01"

#: One prompt for one lead. Long enough for a slow first token on a cold model,
#: short enough that a hung provider does not hold up a batch of hundreds.
TIMEOUT_SECONDS = 90

#: A ceiling, not a target. The prompts in this system ask for a short JSON
#: object or a few sentences; the cap exists so a runaway generation cannot
#: quietly cost a fortune on a metered account.
MAX_OUTPUT_TOKENS = 2048


@dataclass
class ChatReply:
    """The `.content` shape `llm.complete()` reads."""

    content: str
    raw: Any = None


def _split(messages: Iterable[tuple[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """
    Separate the system prompt from the conversation.

    `llm.complete()` hands over [("system", ...), ("human", ...)], and the two
    APIs want that split differently, so it is done once here.
    """
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []
    for role, text in messages:
        if role == "system":
            system_parts.append(text)
        else:
            turns.append({"role": "user" if role in ("human", "user") else role, "content": text})
    return "\n\n".join(system_parts), turns


def _fail(provider: str, response: requests.Response) -> RuntimeError:
    """
    An error the person who pasted the key can act on.

    The provider's own message is included because it is nearly always the
    useful part -- "incorrect API key provided", "insufficient credit",
    "model not found" -- and the alternative is a bare status code that sends
    somebody looking in the wrong place.
    """
    detail = ""
    try:
        payload = response.json()
        error = payload.get("error") or payload
        detail = str(error.get("message") or error.get("type") or "")[:300]
    except ValueError:
        detail = response.text[:300]
    return RuntimeError(f"{provider} returned {response.status_code}: {detail}".strip())


# --------------------------------------------------------------------------- #
# OpenAI-compatible: ChatGPT and DeepSeek
# --------------------------------------------------------------------------- #

class OpenAICompatibleChat:
    """
    Chat completions, as OpenAI defined them and DeepSeek implements them.

    One class for both because the only difference is the base URL. If a third
    service with the same shape shows up, it is a registry entry and a base
    URL, not a new client.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        temperature: float,
        provider: str = "openai",
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.provider = provider

    def invoke(self, messages: list[tuple[str, str]]) -> ChatReply:
        system, turns = _split(messages)
        if system:
            turns = [{"role": "system", "content": system}] + turns

        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": turns,
                "temperature": self.temperature,
                "max_tokens": MAX_OUTPUT_TOKENS,
            },
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise _fail(self.provider, response)

        payload = response.json()
        choices = payload.get("choices") or []
        text = (choices[0].get("message", {}).get("content") or "") if choices else ""
        return ChatReply(content=str(text), raw=payload)


# --------------------------------------------------------------------------- #
# Anthropic
# --------------------------------------------------------------------------- #

class AnthropicChat:
    """
    Claude, over the Messages API.

    Three things differ from the OpenAI shape and all three are load-bearing:
    the header is `x-api-key`, `anthropic-version` is required, and the system
    prompt is a top-level field -- a message with role "system" is rejected.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        temperature: float,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.provider = "anthropic"

    def invoke(self, messages: list[tuple[str, str]]) -> ChatReply:
        system, turns = _split(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": turns,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": self.temperature,
        }
        if system:
            body["system"] = system

        response = requests.post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise _fail(self.provider, response)

        payload = response.json()
        # The response is a list of typed blocks. Only the text ones are ours;
        # joining everything would put block metadata into the prompt output.
        text = "".join(
            block.get("text", "")
            for block in (payload.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return ChatReply(content=text, raw=payload)
