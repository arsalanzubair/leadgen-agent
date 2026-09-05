"""
The providers a user brings their own account for.

Every one of these was listed as "coming soon" and is now real: ChatGPT,
Claude, DeepSeek, any SMTP mailbox, Anymail Finder, HubSpot and Pipedrive.
"Real" has to mean something testable, so what is checked here is the part that
breaks in production and cannot be seen by reading the code:

  * the wire format each vendor actually requires -- Anthropic's system prompt
    is a top-level field and NOT a message, its key goes in `x-api-key`, and it
    needs a version header; OpenAI and DeepSeek want a bearer token and a
    system message
  * that a workspace choosing one of these actually gets it, rather than
    falling through to a free provider
  * that a metered lookup spends its counter BEFORE the call, and that a result
    the service is not sure about is thrown away rather than emailed
  * that a CRM write updates a person rather than creating a fifth copy of them
  * that nothing here is reachable by accident: the default fallback order
    contains no paid provider

No test in this file opens a socket -- `requests` is mocked at the module the
integration calls, which is also how a wrong header would show up here.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from src.integrations import anymail_finder, crm_rest, email_sender, hosted_chat, llm
from src.providers import registry
from src.state import new_lead_state


@pytest.fixture(autouse=True)
def _no_network(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))

    def blocked(*args, **kwargs):
        raise AssertionError(
            "a test tried to open a network connection; mock the integration instead"
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


class FakeResponse:
    """The parts of a requests.Response these integrations actually read."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""
        self.content = b"x"

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# =========================================================================== #
# The hosted chat models
# =========================================================================== #

def test_openai_sends_the_system_prompt_as_a_message(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return FakeResponse(
            payload={"choices": [{"message": {"content": "hello"}}]}
        )

    monkeypatch.setattr(hosted_chat.requests, "post", fake_post)

    client = hosted_chat.OpenAICompatibleChat(
        api_key="sk-test", model="gpt-4o-mini",
        base_url="https://api.openai.com/v1", temperature=0.3,
    )
    reply = client.invoke([("system", "be brief"), ("human", "who are you")])

    assert reply.content == "hello"
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "who are you"}


def test_deepseek_is_the_same_client_pointed_somewhere_else(monkeypatch):
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        return FakeResponse(payload={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(hosted_chat.requests, "post", fake_post)

    client = hosted_chat.OpenAICompatibleChat(
        api_key="sk", model="deepseek-chat",
        base_url="https://api.deepseek.com/v1", temperature=0.1,
        provider="DeepSeek",
    )
    assert client.invoke([("human", "hi")]).content == "ok"
    assert seen["url"].startswith("https://api.deepseek.com/v1")


def test_anthropic_puts_the_system_prompt_at_the_top_level(monkeypatch):
    """
    The Messages API REJECTS a message with role "system". Getting this wrong
    is a 400 on every single call, so it is worth a test of its own.
    """
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return FakeResponse(
            payload={"content": [{"type": "text", "text": "hello there"}]}
        )

    monkeypatch.setattr(hosted_chat.requests, "post", fake_post)

    client = hosted_chat.AnthropicChat(
        api_key="sk-ant-test", model="claude-sonnet-4-5",
        base_url="https://api.anthropic.com", temperature=0.2,
    )
    reply = client.invoke([("system", "be brief"), ("human", "who are you")])

    assert reply.content == "hello there"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-test"
    assert seen["headers"]["anthropic-version"] == hosted_chat.ANTHROPIC_VERSION
    assert "Authorization" not in seen["headers"]
    assert seen["body"]["system"] == "be brief"
    assert all(turn["role"] != "system" for turn in seen["body"]["messages"])


def test_anthropic_reads_only_the_text_blocks(monkeypatch):
    monkeypatch.setattr(
        hosted_chat.requests, "post",
        lambda *a, **k: FakeResponse(
            payload={
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "the answer"},
                ]
            }
        ),
    )
    client = hosted_chat.AnthropicChat(
        api_key="k", model="m", base_url="https://api.anthropic.com", temperature=0,
    )
    assert client.invoke([("human", "q")]).content == "the answer"


def test_a_rejected_key_says_what_the_provider_said(monkeypatch):
    monkeypatch.setattr(
        hosted_chat.requests, "post",
        lambda *a, **k: FakeResponse(
            status_code=401,
            payload={"error": {"message": "Incorrect API key provided"}},
        ),
    )
    client = hosted_chat.OpenAICompatibleChat(
        api_key="wrong", model="m", base_url="https://api.openai.com/v1",
        temperature=0, provider="OpenAI",
    )
    with pytest.raises(RuntimeError, match="Incorrect API key provided"):
        client.invoke([("human", "q")])


# =========================================================================== #
# Choosing one of them actually gets you it
# =========================================================================== #

def test_choosing_claude_runs_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(
        hosted_chat.requests, "post",
        lambda *a, **k: FakeResponse(payload={"content": [{"type": "text", "text": "drafted"}]}),
    )
    llm._build_client.cache_clear()

    result = llm.complete("write something", task="test", chain=("anthropic",))

    assert result.provider == "anthropic"
    assert result.text == "drafted"


def test_the_default_chain_never_reaches_a_paid_provider():
    """
    A workspace that has not chosen to spend money must not start spending it
    because a free tier was rate-limited.
    """
    assert not set(llm.FALLBACK_ORDER) & llm.PAID_PROVIDERS
    assert set(llm.resolve_provider_chain()).isdisjoint(llm.PAID_PROVIDERS)


def test_a_named_paid_provider_is_not_filtered_out_of_its_own_chain():
    """
    The chain used to be filtered against FALLBACK_ORDER, which would have
    dropped every one of these silently and run Groq instead.
    """
    for name in ("openai", "anthropic", "deepseek"):
        assert llm.resolve_provider_chain(name)[0] == name


def test_the_capability_adapter_keeps_the_workspaces_own_choice():
    """
    The same trap, one layer up, and the one that actually reached users: the
    adapter built from the registry filtered the tenant's primary and fallback
    against FALLBACK_ORDER. A workspace that connected Claude and selected it
    would have been writing with Groq and had no way to tell.
    """
    from src.providers.llm_adapters import LLMAdapter

    adapter = LLMAdapter("anthropic", fallback="openai")
    assert adapter.chain[0] == "anthropic"
    assert adapter.chain[1] == "openai"
    # ...and the tail behind them is still free-only.
    assert set(adapter.chain[2:]).isdisjoint(llm.PAID_PROVIDERS)


def test_an_unknown_provider_id_is_dropped_rather_than_tried():
    """A typo in the config must not spend a slot on a guaranteed failure."""
    from src.providers.llm_adapters import LLMAdapter

    assert "nonsense" not in LLMAdapter("nonsense").chain


# =========================================================================== #
# Any SMTP mailbox
# =========================================================================== #

def test_smtp_sender_reads_its_own_settings(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "mail.example.com")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USERNAME", "me@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")

    sender = email_sender.SMTPSender("t")
    assert sender._connection() == ("mail.example.com", 2525, "me@example.com", "secret")


def test_smtp_says_what_is_missing_rather_than_failing_at_send_time(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        email_sender.SMTPSender("t")._connection()


def test_an_unconfigured_smtp_choice_degrades_to_printing(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    sender = email_sender.get_sender("t", provider="smtp")
    assert isinstance(sender, email_sender.DryRunSender)


# =========================================================================== #
# Anymail Finder
# =========================================================================== #

def test_a_lookup_spends_the_counter_before_the_call(monkeypatch):
    monkeypatch.setenv("ANYMAIL_FINDER_API_KEY", "key")
    monkeypatch.setattr(
        anymail_finder.requests, "post",
        lambda *a, **k: FakeResponse(
            payload={"results": {"email": "a@b.com", "validation": "valid"}}
        ),
    )

    before = anymail_finder.remaining_lookups()
    found = anymail_finder.find_email("b.com", first_name="A", last_name="B")

    assert found is not None
    assert found.email == "a@b.com"
    assert anymail_finder.remaining_lookups() == before - 1


def test_a_risky_address_is_thrown_away(monkeypatch):
    """A bounce costs sender reputation, which is worth more than one lead."""
    monkeypatch.setenv("ANYMAIL_FINDER_API_KEY", "key")
    monkeypatch.setattr(
        anymail_finder.requests, "post",
        lambda *a, **k: FakeResponse(
            payload={"results": {"email": "guess@b.com", "validation": "risky"}}
        ),
    )
    assert anymail_finder.find_email("b.com", first_name="A", last_name="B") is None


def test_the_monthly_cap_stops_the_lookups(monkeypatch):
    monkeypatch.setenv("ANYMAIL_FINDER_API_KEY", "key")
    monkeypatch.setenv("ANYMAIL_FINDER_MONTHLY_LOOKUP_CAP", "1")
    monkeypatch.setattr(
        anymail_finder.requests, "post",
        lambda *a, **k: FakeResponse(
            payload={"results": {"email": "a@b.com", "validation": "valid"}}
        ),
    )

    assert anymail_finder.find_email("b.com", first_name="A", last_name="B") is not None
    assert anymail_finder.find_email("c.com", first_name="C", last_name="D") is None


# =========================================================================== #
# HubSpot and Pipedrive
# =========================================================================== #

def _lead(**over):
    state = new_lead_state(
        tenant_id="example_tenant", niche_id="local_dental", region="US",
        company_name="Northgate Dental",
    )
    state.update(
        contact_name="Dana Whitfield",
        contact_email="dana@northgate.example",
        website="https://northgate.example",
        fit_score=91,
        fit_reason="matches four signals",
        **over,
    )
    return state


def test_hubspot_creates_a_contact_with_the_summary(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, kwargs.get("json") or {}))
        if url.endswith("/search"):
            return FakeResponse(payload={"results": []})
        return FakeResponse(payload={"id": "42"})

    monkeypatch.setenv("HUBSPOT_API_KEY", "pat-test")
    monkeypatch.setattr(crm_rest.requests, "request", fake_request)

    outcome = crm_rest.HubSpotCRM("example_tenant").upsert_lead(_lead())

    assert outcome == "created"
    write = [c for c in calls if c[0] == "POST" and c[1].endswith("/contacts")][0]
    properties = write[2]["properties"]
    assert properties["email"] == "dana@northgate.example"
    assert properties["firstname"] == "Dana"
    assert properties["lastname"] == "Whitfield"
    assert properties["company"] == "Northgate Dental"
    assert "fit reason: matches four signals" in properties["leadflow_summary"]


def test_hubspot_updates_rather_than_duplicating(monkeypatch):
    methods: list[str] = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        methods.append(f"{method} {url.rsplit('/', 1)[-1]}")
        if url.endswith("/search"):
            return FakeResponse(payload={"results": [{"id": "77"}]})
        return FakeResponse(payload={"id": "77"})

    monkeypatch.setenv("HUBSPOT_API_KEY", "pat-test")
    monkeypatch.setattr(crm_rest.requests, "request", fake_request)

    assert crm_rest.HubSpotCRM("example_tenant").upsert_lead(_lead()) == "updated"
    assert any(m.startswith("PATCH") for m in methods)
    assert not any(m == "POST contacts" for m in methods)


def test_hubspot_still_saves_the_contact_without_the_custom_property(monkeypatch):
    """
    An account with no `leadflow_summary` property must not lose the lead over
    a field nobody created.
    """
    bodies: list[dict] = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        body = kwargs.get("json") or {}
        if url.endswith("/search"):
            return FakeResponse(payload={"results": []})
        bodies.append(body)
        if "leadflow_summary" in body.get("properties", {}):
            return FakeResponse(
                status_code=400,
                payload={"message": "PROPERTY_DOESNT_EXIST"},
                text="PROPERTY_DOESNT_EXIST",
            )
        return FakeResponse(payload={"id": "9"})

    monkeypatch.setenv("HUBSPOT_API_KEY", "pat-test")
    monkeypatch.setattr(crm_rest.requests, "request", fake_request)

    assert crm_rest.HubSpotCRM("example_tenant").upsert_lead(_lead()) == "created"
    assert any("leadflow_summary" not in b.get("properties", {}) for b in bodies)


def test_pipedrive_creates_a_person_and_attaches_the_summary(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, params=None, timeout=None, **kwargs):
        calls.append((method, url))
        if "/persons/search" in url:
            return FakeResponse(payload={"data": {"items": []}})
        if url.endswith("/persons"):
            return FakeResponse(payload={"data": {"id": 5}})
        return FakeResponse(payload={"data": {"id": 1}})

    monkeypatch.setenv("PIPEDRIVE_API_KEY", "token")
    monkeypatch.setenv("PIPEDRIVE_DOMAIN", "acme")
    monkeypatch.setattr(crm_rest.requests, "request", fake_request)

    assert crm_rest.PipedriveCRM("example_tenant").upsert_lead(_lead()) == "created"
    assert any(url.endswith("/notes") for _, url in calls)


def test_pipedrive_never_puts_the_token_in_an_error(monkeypatch):
    def fake_request(method, url, params=None, timeout=None, **kwargs):
        return FakeResponse(status_code=401, text="Unauthorized")

    monkeypatch.setenv("PIPEDRIVE_API_KEY", "super-secret-token")
    monkeypatch.setenv("PIPEDRIVE_DOMAIN", "acme")
    monkeypatch.setattr(crm_rest.requests, "request", fake_request)

    with pytest.raises(RuntimeError) as caught:
        crm_rest.PipedriveCRM("example_tenant")._request("GET", "/users/me")
    assert "super-secret-token" not in str(caught.value)


# =========================================================================== #
# The registry keeps its promises
# =========================================================================== #

def test_nothing_is_listed_that_is_not_built():
    """
    "Coming soon" is gone. Anything a user can see in a dropdown has an
    adapter, a credential check and a real call behind it.
    """
    unbuilt = [spec.id for spec in registry.PROVIDERS if not spec.enabled]
    assert unbuilt == []


def test_every_provider_that_needs_a_key_can_be_tested_before_it_is_saved():
    from backend import providers as backend_providers

    for spec in registry.PROVIDERS:
        if spec.hidden:
            continue
        # Raises if a credential-needing provider has no check, which is what
        # stops an unverified key being stored.
        assert backend_providers._check_for(spec) is not None


def test_the_new_providers_are_selectable_for_their_capability():
    expected = {
        registry.Capability.LLM: {"openai", "anthropic", "deepseek"},
        registry.Capability.ENRICHMENT: {"anymail_finder"},
        registry.Capability.EMAIL_SENDER: {"smtp"},
        registry.Capability.CRM: {"hubspot", "pipedrive"},
    }
    for capability, ids in expected.items():
        selectable = set(registry.selectable_ids(capability))
        assert ids <= selectable, f"{capability.value} is missing {ids - selectable}"
