"""
registry.py -- the one list of who can do what.

THIS IS THE SINGLE SOURCE OF TRUTH for providers. `backend/providers.py`
imports from here and attaches a credential test to each entry;
`GET /api/providers` and `GET /api/integrations` both serialise from here. If
you find a vendor name hardcoded in a router or a React component, that is a
bug -- adding a provider must be an edit to this file and nothing else.

Each entry declares what the capability layer needs (which interface it
implements, how to build it) and what the Connections screen needs (what to
call it, what fields to ask for, where to get a key, what the free tier is).

`enabled=False` would mean "we have left room for this, it is not built".
Nothing uses it: every provider listed here has an adapter, a credential check
and a real call behind it. The flag stays because the alternative to listing an
unbuilt provider honestly is listing it dishonestly. It is
listed so the dropdown can show what is coming without lying about what works;
`build()` on a disabled provider raises rather than returning something that
silently does nothing. That distinction matters more than it looks: a
"connected" provider that no-ops would show a green tick on Connections and
produce an empty batch three hours later.

`hidden=True` means "real and resolvable, but not a thing a user connects" --
the dry-run sender, the null reply reader, the local CSV fallback, the
deterministic test model. They need registry entries because the resolver
resolves them; they must not appear on Connections because there is nothing to
connect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.providers.base import Capability, CredentialType

__all__ = [
    "Capability",
    "ConnectionField",
    "CredentialType",
    "ENV_VAR_SOURCES",
    "PROVIDERS",
    "PROVIDERS_BY_ID",
    "ProviderNotBuilt",
    "ProviderSpec",
    "UnknownProvider",
    "default_for",
    "for_capability",
    "get",
    "is_selectable",
    "load_factory",
    "selectable_ids",
]

# --------------------------------------------------------------------------- #
# Entry shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConnectionField:
    """One thing the user has to paste in. Mirrors the dashboard's ProviderField."""

    name: str
    label: str
    kind: str = "secret"          # "secret" | "text"
    placeholder: str = ""
    hint: str = ""
    required: bool = True
    multiline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "placeholder": self.placeholder,
            "hint": self.hint,
            "required": self.required,
            "multiline": self.multiline,
        }


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    capability: Capability
    credential_type: CredentialType
    purpose: str = ""
    free_tier: str = ""
    docs_label: str = ""
    docs_url: str = ""
    connection_fields: tuple[ConnectionField, ...] = ()
    #: field name -> env var it satisfies, for the encrypted secrets store.
    env_vars: dict[str, str] = field(default_factory=dict)
    #: False for "coming soon": listed, never resolvable.
    enabled: bool = True
    #: True for internal adapters that are resolvable but never connectable.
    hidden: bool = False
    #: True when its capability cannot run at all without something connected.
    essential: bool = False
    #: Dotted path to the adapter factory, resolved lazily. Deferring the
    #: import is what keeps `import src.providers.registry` free of httpx,
    #: gspread, langchain and the rest -- the settings service imports this
    #: module to render a form and must not drag the whole agent in with it.
    factory: str = ""
    #: Set on the generic REST adapter so callers can ask for extra config.
    is_custom: bool = False
    #: Alternative credential sets, any ONE of which makes this usable. Used
    #: where a provider can borrow another's credentials -- the IMAP reader
    #: falls back to the Gmail address and app password, which is why its own
    #: fields are all optional. Empty means "judge it by the required fields".
    requires_any: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.display_name,
            "capability": self.capability.value,
            "interface": self.capability.interface,
            "credential_type": self.credential_type.value,
            "purpose": self.purpose,
            "free_tier": self.free_tier,
            "help_label": self.docs_label,
            "help_url": self.docs_url,
            "fields": [f.to_dict() for f in self.connection_fields],
            "enabled": self.enabled,
            "essential": self.essential,
            "is_custom": self.is_custom,
            "needs_credentials": self.credential_type is not CredentialType.NONE,
        }


# --------------------------------------------------------------------------- #
# Reusable field sets
# --------------------------------------------------------------------------- #

#: Every custom provider asks the same four things. The endpoint has to speak
#: the JSON contract documented in src/providers/custom.py -- which is why the
#: hint says so rather than implying any URL will work.
CUSTOM_FIELDS: tuple[ConnectionField, ...] = (
    ConnectionField(
        name="base_url",
        label="Endpoint URL",
        kind="text",
        placeholder="https://api.example.com/v1/...",
        hint="Must be https. Called from this machine only, never from the browser.",
    ),
    ConnectionField(
        name="auth_style",
        label="How it authenticates",
        kind="text",
        placeholder="bearer",
        hint="bearer, header, query or none. See the setup notes for what each sends.",
        required=False,
    ),
    ConnectionField(
        name="token",
        label="Key or token",
        placeholder="",
        hint="Stored encrypted like every other key, and never sent to the browser.",
        required=False,
    ),
    ConnectionField(
        name="headers",
        label="Extra headers",
        kind="text",
        placeholder='{"X-Org": "acme"}',
        hint="Optional JSON object. Values are never written to the log.",
        required=False,
        multiline=True,
    ),
)


def _custom(
    capability: Capability, purpose: str, *, docs: str = ""
) -> ProviderSpec:
    """The generic REST adapter for one capability."""
    return ProviderSpec(
        id=f"custom_{capability.value}",
        display_name="Custom endpoint",
        capability=capability,
        credential_type=CredentialType.CUSTOM_REST,
        purpose=purpose,
        free_tier="Whatever your own endpoint costs",
        docs_label=docs or "How to make an endpoint this can call",
        docs_url="",
        connection_fields=CUSTOM_FIELDS,
        # One token per capability, so pointing the model and the CRM at two
        # different endpoints does not make them share a key.
        env_vars={"token": f"CUSTOM_TOKEN__{capability.value}"},
        factory="src.providers.custom:build",
        is_custom=True,
    )


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

PROVIDERS: tuple[ProviderSpec, ...] = (

    # -- Something to write with -------------------------------------------- #
    ProviderSpec(
        id="groq",
        display_name="Groq",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        purpose="Reads your requests, scores each business and writes your outreach",
        free_tier="Free, generous rate limits",
        docs_label="Create a free key at console.groq.com",
        docs_url="https://console.groq.com/keys",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Groq API key",
                placeholder="gsk_...",
                hint="Starts with gsk_. Free to create, no card needed.",
            ),
        ),
        env_vars={"api_key": "GROQ_API_KEY"},
        essential=True,
        factory="src.providers.llm_adapters:build",
    ),
    ProviderSpec(
        id="gemini",
        display_name="Google Gemini",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        purpose="Reads your requests, scores each business and writes your outreach",
        free_tier="Free tier through Google AI Studio",
        docs_label="Create a free key at aistudio.google.com",
        docs_url="https://aistudio.google.com/app/apikey",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Google AI Studio key",
                placeholder="AIza...",
                hint="From AI Studio, not the Cloud console.",
            ),
        ),
        env_vars={"api_key": "GOOGLE_API_KEY"},
        essential=True,
        factory="src.providers.llm_adapters:build",
    ),
    ProviderSpec(
        id="ollama",
        display_name="Ollama (on this machine)",
        capability=Capability.LLM,
        credential_type=CredentialType.NONE,
        purpose="A model running on your own computer. Nothing leaves the building",
        free_tier="Free - it is your own hardware",
        docs_label="Install it from ollama.com",
        docs_url="https://ollama.com",
        connection_fields=(
            ConnectionField(
                name="base_url",
                label="Where it is listening",
                kind="text",
                placeholder="http://localhost:11434",
                hint="Leave blank unless you changed the default port.",
                required=False,
            ),
        ),
        env_vars={"base_url": "OLLAMA_BASE_URL"},
        factory="src.providers.llm_adapters:build",
    ),
    ProviderSpec(
        id="openai",
        display_name="ChatGPT",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        purpose="OpenAI's models, on your own account",
        free_tier="Pay as you go - you are billed by OpenAI, not by us",
        docs_label="Create a key at platform.openai.com",
        docs_url="https://platform.openai.com/api-keys",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="OpenAI API key",
                placeholder="sk-...",
                hint="From platform.openai.com, not from a ChatGPT subscription.",
            ),
        ),
        env_vars={"api_key": "OPENAI_API_KEY"},
        factory="src.providers.llm_adapters:build",
    ),
    ProviderSpec(
        id="anthropic",
        display_name="Claude",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        purpose="Anthropic's models, on your own account",
        free_tier="Pay as you go - you are billed by Anthropic, not by us",
        docs_label="Create a key at console.anthropic.com",
        docs_url="https://console.anthropic.com/settings/keys",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Anthropic API key",
                placeholder="sk-ant-...",
                hint="From the Anthropic console, not from a Claude subscription.",
            ),
        ),
        env_vars={"api_key": "ANTHROPIC_API_KEY"},
        factory="src.providers.llm_adapters:build",
    ),
    ProviderSpec(
        id="deepseek",
        display_name="DeepSeek",
        capability=Capability.LLM,
        credential_type=CredentialType.API_KEY,
        purpose="Cheap to run, and speaks the same API as ChatGPT",
        free_tier="Pay as you go - among the least expensive of these",
        docs_label="Create a key at platform.deepseek.com",
        docs_url="https://platform.deepseek.com/api_keys",
        connection_fields=(
            ConnectionField(name="api_key", label="DeepSeek API key", placeholder="sk-..."),
        ),
        env_vars={"api_key": "DEEPSEEK_API_KEY"},
        factory="src.providers.llm_adapters:build",
    ),
    _custom(
        Capability.LLM,
        "Any endpoint that speaks the OpenAI chat-completions shape",
    ),
    ProviderSpec(
        id="mock",
        display_name="Built-in test model",
        capability=Capability.LLM,
        credential_type=CredentialType.NONE,
        purpose="Deterministic canned answers, for Test Mode and the test suite",
        hidden=True,
        factory="src.providers.llm_adapters:build",
    ),

    # -- Somewhere to find local businesses --------------------------------- #
    ProviderSpec(
        id="osm",
        display_name="OpenStreetMap",
        capability=Capability.DISCOVERY_LOCAL,
        credential_type=CredentialType.NONE,
        purpose="Finds shops, clinics, trades and restaurants by area. No key needed",
        free_tier="Free, rate limited to one search a second",
        docs_label="About the OpenStreetMap search service",
        docs_url="https://operations.osmfoundation.org/policies/nominatim/",
        factory="src.providers.discovery_adapters:build",
        essential=True,
    ),
    _custom(
        Capability.DISCOVERY_LOCAL,
        "Your own source of local businesses",
    ),

    # -- Somewhere to find people at companies ------------------------------ #
    ProviderSpec(
        id="apollo",
        display_name="Apollo",
        capability=Capability.DISCOVERY_B2B,
        credential_type=CredentialType.API_KEY,
        purpose="Finds people by job title at companies, rather than places on a map",
        free_tier="Free plan with a monthly credit allowance",
        docs_label="Get a key from your Apollo settings",
        docs_url="https://developer.apollo.io/keys/",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Apollo API key",
                placeholder="",
                hint="Found under Settings, then Integrations, then API.",
            ),
        ),
        env_vars={"api_key": "APOLLO_API_KEY"},
        factory="src.providers.discovery_adapters:build",
    ),
    ProviderSpec(
        id="csv_import",
        display_name="A spreadsheet you export yourself",
        capability=Capability.DISCOVERY_B2B,
        credential_type=CredentialType.NONE,
        purpose="Reads CSV exports you drop into a folder. No account, no key, no limit",
        free_tier="Free",
        docs_label="Where to put the files",
        docs_url="",
        factory="src.providers.discovery_adapters:build",
    ),
    _custom(
        Capability.DISCOVERY_B2B,
        "Your own source of business contacts",
    ),

    # -- Finding the right person ------------------------------------------- #
    ProviderSpec(
        id="hunter",
        display_name="Hunter",
        capability=Capability.ENRICHMENT,
        credential_type=CredentialType.API_KEY,
        purpose="Turns a company into a named person with a real email address",
        free_tier="25 lookups a month, and this product will not go over it",
        docs_label="Create a free key at hunter.io",
        docs_url="https://hunter.io/api-keys",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Hunter API key",
                placeholder="",
                hint="The free plan allows 25 lookups a month; the agent stops at 25.",
            ),
        ),
        env_vars={"api_key": "HUNTER_API_KEY"},
        factory="src.providers.enrichment_adapters:build",
    ),
    ProviderSpec(
        id="anymail_finder",
        display_name="Anymail Finder",
        capability=Capability.ENRICHMENT,
        credential_type=CredentialType.API_KEY,
        purpose="Finds a named person's address, the same job as Hunter",
        free_tier="Paid, charged per verified address - capped at 100 lookups a month",
        docs_label="Create a key at anymailfinder.com",
        docs_url="https://anymailfinder.com/api",
        connection_fields=(
            ConnectionField(name="api_key", label="Anymail Finder API key", placeholder=""),
        ),
        env_vars={"api_key": "ANYMAIL_FINDER_API_KEY"},
        factory="src.providers.enrichment_adapters:build",
    ),
    ProviderSpec(
        id="website_only",
        display_name="Read their website only",
        capability=Capability.ENRICHMENT,
        credential_type=CredentialType.NONE,
        purpose="Uses only what is published on the business's own site. Always on anyway",
        free_tier="Free",
        factory="src.providers.enrichment_adapters:build",
    ),
    _custom(
        Capability.ENRICHMENT,
        "Your own contact-lookup service",
    ),

    # -- Sending the email --------------------------------------------------- #
    ProviderSpec(
        id="gmail_smtp",
        display_name="Gmail",
        capability=Capability.EMAIL_SENDER,
        credential_type=CredentialType.EMAIL_PASSWORD,
        purpose="Sends your outreach from your own mailbox",
        free_tier="Around 500 a day; the agent is capped far below that on purpose",
        docs_label="Create an app password for your Google account",
        docs_url="https://myaccount.google.com/apppasswords",
        connection_fields=(
            ConnectionField(
                name="address",
                label="Your Gmail address",
                kind="text",
                placeholder="you@yourcompany.com",
                hint="A Google Workspace address on your own domain works too.",
            ),
            ConnectionField(
                name="app_password",
                label="App password",
                placeholder="16 characters",
                hint="Not your normal password. Two-step verification must be on to create one.",
            ),
        ),
        env_vars={"address": "GMAIL_ADDRESS", "app_password": "GMAIL_APP_PASSWORD"},
        factory="src.providers.email_adapters:build",
    ),
    ProviderSpec(
        id="brevo",
        display_name="Brevo",
        capability=Capability.EMAIL_SENDER,
        credential_type=CredentialType.API_KEY,
        purpose="Sends through Brevo instead of your own mailbox",
        free_tier="300 a day on the free plan",
        docs_label="Create an API key in your Brevo account",
        docs_url="https://app.brevo.com/settings/keys/api",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Brevo API key",
                placeholder="xkeysib-...",
                hint="Starts with xkeysib-. The v3 key, not an SMTP password.",
            ),
        ),
        env_vars={"api_key": "BREVO_API_KEY"},
        factory="src.providers.email_adapters:build",
    ),
    ProviderSpec(
        id="smtp",
        display_name="Any SMTP mailbox",
        capability=Capability.EMAIL_SENDER,
        credential_type=CredentialType.EMAIL_PASSWORD,
        purpose="Your own mail server, a work address, or a hosting account",
        free_tier="Whatever your mail host allows - the daily cap here still applies",
        docs_label="Your mail host publishes these settings",
        connection_fields=(
            ConnectionField(name="host", label="Server", placeholder="smtp.example.com"),
            ConnectionField(name="port", label="Port", placeholder="587"),
            ConnectionField(name="username", label="Username", placeholder=""),
            ConnectionField(
                name="password", label="Password", kind="password", placeholder=""
            ),
        ),
        env_vars={
            "host": "SMTP_HOST",
            "port": "SMTP_PORT",
            "username": "SMTP_USERNAME",
            "password": "SMTP_PASSWORD",
        },
        factory="src.providers.email_adapters:build",
    ),
    _custom(
        Capability.EMAIL_SENDER,
        "Your own sending service",
    ),
    ProviderSpec(
        id="dry_run",
        display_name="Write it down instead of sending",
        capability=Capability.EMAIL_SENDER,
        credential_type=CredentialType.NONE,
        purpose="What Test Mode uses, and what an unconnected workspace falls back to",
        hidden=True,
        factory="src.providers.email_adapters:build",
    ),

    # -- Reading the replies ------------------------------------------------- #
    ProviderSpec(
        id="imap",
        display_name="Your mailbox (IMAP)",
        capability=Capability.EMAIL_READER,
        credential_type=CredentialType.EMAIL_PASSWORD,
        purpose="Watches for replies so a conversation stops the follow-ups",
        free_tier="Free - it is your own mailbox",
        docs_label="Find your provider's IMAP server name",
        docs_url="",
        connection_fields=(
            ConnectionField(
                name="host",
                label="IMAP server",
                kind="text",
                placeholder="imap.gmail.com",
                hint="Leave blank for Gmail.",
                required=False,
            ),
            ConnectionField(
                name="username",
                label="Mailbox address",
                kind="text",
                placeholder="you@yourcompany.com",
                hint="Leave blank to reuse the Gmail address you connected for sending.",
                required=False,
            ),
            ConnectionField(
                name="password",
                label="App password",
                placeholder="",
                hint="Leave blank to reuse the Gmail app password.",
                required=False,
            ),
        ),
        env_vars={
            "host": "IMAP_HOST",
            "username": "IMAP_USERNAME",
            "password": "IMAP_PASSWORD",
        },
        # Its own credentials, or the ones already given for sending. Every
        # field above is optional precisely so the second case needs no typing.
        requires_any=(
            ("IMAP_USERNAME", "IMAP_PASSWORD"),
            ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"),
        ),
        factory="src.providers.email_adapters:build",
    ),
    _custom(
        Capability.EMAIL_READER,
        "Your own reply feed",
    ),
    ProviderSpec(
        id="none",
        display_name="Do not watch for replies",
        capability=Capability.EMAIL_READER,
        credential_type=CredentialType.NONE,
        purpose="What Test Mode uses, and the fallback when no mailbox is connected",
        hidden=True,
        factory="src.providers.email_adapters:build",
    ),

    # -- Keeping your records ------------------------------------------------ #
    ProviderSpec(
        id="google_sheets",
        display_name="Google Sheets",
        capability=Capability.CRM,
        credential_type=CredentialType.SERVICE_ACCOUNT_JSON,
        purpose="Writes every business and outcome to a spreadsheet you own",
        free_tier="Free with a Google service account",
        docs_label="Create a service account and download its JSON key",
        docs_url="https://console.cloud.google.com/iam-admin/serviceaccounts",
        connection_fields=(
            ConnectionField(
                name="spreadsheet_id",
                label="Spreadsheet ID",
                kind="text",
                placeholder="1AbC...",
                hint="The long string in the sheet's URL between /d/ and /edit.",
            ),
            ConnectionField(
                name="service_account_json",
                label="Service account key",
                placeholder='{ "type": "service_account", ... }',
                hint="Paste the whole JSON file. Then share the sheet with the address inside it.",
                multiline=True,
            ),
        ),
        env_vars={"spreadsheet_id": "GOOGLE_SHEETS_SPREADSHEET_ID"},
        factory="src.providers.crm_adapters:build",
    ),
    ProviderSpec(
        id="airtable",
        display_name="Airtable",
        capability=Capability.CRM,
        credential_type=CredentialType.API_KEY,
        purpose="Same records, in an Airtable base instead of a spreadsheet",
        free_tier="Free plan, 1,000 records a base",
        docs_label="Create a personal access token",
        docs_url="https://airtable.com/create/tokens",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Personal access token",
                placeholder="pat...",
                hint="Needs data.records:write on the base you are using.",
            ),
            ConnectionField(
                name="base_id",
                label="Base ID",
                kind="text",
                placeholder="app...",
                hint="Starts with app. From the base's API page.",
            ),
        ),
        env_vars={"api_key": "AIRTABLE_API_KEY", "base_id": "AIRTABLE_BASE_ID"},
        factory="src.providers.crm_adapters:build",
    ),
    ProviderSpec(
        id="csv",
        display_name="A CSV file on this machine",
        capability=Capability.CRM,
        credential_type=CredentialType.NONE,
        purpose="No account needed. Rows are appended to a file in your data folder",
        free_tier="Free",
        factory="src.providers.crm_adapters:build",
    ),
    ProviderSpec(
        id="hubspot",
        display_name="HubSpot",
        capability=Capability.CRM,
        credential_type=CredentialType.API_KEY,
        purpose="Writes each lead in as a contact, with the match reason attached",
        free_tier="HubSpot's free CRM is enough for this",
        docs_label="Settings -> Integrations -> Private Apps",
        docs_url="https://developers.hubspot.com/docs/api/private-apps",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="Private app token",
                placeholder="pat-...",
                hint="Give the private app the crm.objects.contacts read and write scopes.",
            ),
        ),
        env_vars={"api_key": "HUBSPOT_API_KEY"},
        factory="src.providers.crm_adapters:build",
    ),
    ProviderSpec(
        id="pipedrive",
        display_name="Pipedrive",
        capability=Capability.CRM,
        credential_type=CredentialType.API_KEY,
        purpose="Writes each lead in as a person, with the match reason as a note",
        free_tier="Paid, including the trial",
        docs_label="Personal preferences -> API in Pipedrive",
        docs_url="https://pipedrive.readme.io/docs/how-to-find-the-api-token",
        connection_fields=(
            ConnectionField(name="api_key", label="API token", placeholder=""),
            ConnectionField(
                name="domain",
                label="Your Pipedrive address",
                kind="text",
                placeholder="acme",
                hint="Just the first part: for acme.pipedrive.com, type acme.",
            ),
        ),
        env_vars={"api_key": "PIPEDRIVE_API_KEY", "domain": "PIPEDRIVE_DOMAIN"},
        factory="src.providers.crm_adapters:build",
    ),
    _custom(
        Capability.CRM,
        "Your own records endpoint",
    ),

    # -- Writing in other languages ------------------------------------------ #
    ProviderSpec(
        id="deepl",
        display_name="DeepL",
        capability=Capability.TRANSLATION,
        credential_type=CredentialType.API_KEY,
        purpose="Writes your outreach in the recipient's own language",
        free_tier="500,000 characters a month, and this product will not go over it",
        docs_label="Create a free key at deepl.com",
        docs_url="https://www.deepl.com/pro-api",
        connection_fields=(
            ConnectionField(
                name="api_key",
                label="DeepL API key",
                placeholder="...:fx",
                hint="Free keys end in :fx. Both free and paid keys work.",
            ),
        ),
        env_vars={"api_key": "DEEPL_API_KEY"},
        factory="src.providers.translation_adapters:build",
    ),
    ProviderSpec(
        id="llm",
        display_name="Use your writing connection",
        capability=Capability.TRANSLATION,
        credential_type=CredentialType.NONE,
        purpose="Translates with the same model that writes the outreach. No extra account",
        free_tier="Included in whatever you connected to write with",
        factory="src.providers.translation_adapters:build",
    ),
    _custom(
        Capability.TRANSLATION,
        "Your own translation endpoint",
    ),
)


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #

PROVIDERS_BY_ID: dict[str, ProviderSpec] = {spec.id: spec for spec in PROVIDERS}

#: env var -> (provider id, field name). Lets `src.settings` answer "is there a
#: stored secret that satisfies GROQ_API_KEY?" without knowing the registry.
#:
#: Disabled providers are included on purpose: a key stored before a provider
#: was disabled must still resolve, and the env var mapping is not a claim that
#: an adapter exists.
ENV_VAR_SOURCES: dict[str, tuple[str, str]] = {
    env_var: (spec.id, field_name)
    for spec in PROVIDERS
    for field_name, env_var in spec.env_vars.items()
}


class UnknownProvider(KeyError):
    """No registry entry with that id."""


class ProviderNotBuilt(RuntimeError):
    """A registry entry exists but is marked `enabled=False`."""


def get(provider_id: str) -> ProviderSpec:
    spec = PROVIDERS_BY_ID.get(provider_id)
    if spec is None:
        raise UnknownProvider(provider_id)
    return spec


def for_capability(
    capability: Capability | str, *, include_hidden: bool = False
) -> list[ProviderSpec]:
    """Every provider that can do this job, in registry order."""
    wanted = Capability(capability) if isinstance(capability, str) else capability
    return [
        spec
        for spec in PROVIDERS
        if spec.capability is wanted and (include_hidden or not spec.hidden)
    ]


def default_for(capability: Capability | str) -> str:
    """
    The provider a workspace gets when it has not chosen one.

    First enabled, non-hidden entry in registry order -- which is why the
    registry is ordered with the current seeded default first in each block.
    """
    options = for_capability(capability)
    for spec in options:
        if spec.enabled and not spec.is_custom:
            return spec.id
    return options[0].id if options else ""


def is_selectable(provider_id: str, capability: Capability | str) -> bool:
    """True when this id can serve this capability right now."""
    try:
        spec = get(provider_id)
    except UnknownProvider:
        return False
    wanted = Capability(capability) if isinstance(capability, str) else capability
    return spec.capability is wanted and spec.enabled


def selectable_ids(capability: Capability | str) -> list[str]:
    """Ids a tenant config is allowed to name for this capability."""
    wanted = Capability(capability) if isinstance(capability, str) else capability
    return [
        spec.id
        for spec in for_capability(wanted, include_hidden=True)
        if spec.enabled
    ]


def load_factory(spec: ProviderSpec) -> Callable[..., Any]:
    """
    Import the adapter factory named by `spec.factory`.

    Late and by name, so this module stays importable by the settings service
    without pulling in langchain, gspread or httpx.
    """
    if not spec.enabled:
        raise ProviderNotBuilt(
            f"{spec.display_name} is listed but not built yet, so it cannot run."
        )
    if not spec.factory:
        raise ProviderNotBuilt(f"{spec.id} has no adapter.")

    import importlib

    module_path, _, attr = spec.factory.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, attr)
