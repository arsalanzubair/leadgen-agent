"""
base.py -- one interface per capability, and the vocabulary they share.

A node needs "something that can write text" or "somewhere to find local
businesses". It does not need to know that the something is Groq, or that the
somewhere is OpenStreetMap. Every node in this pipeline resolves a capability
from the tenant's configuration and calls the interface; the vendor name
appears in exactly two places, the registry and the adapter that wraps it.

WHY THIS EXISTS

Before this layer, each node imported its vendor module directly and each
vendor module decided for itself which service to use, by asking "is there an
API key for me in the environment?". That made the provider choice a property
of the deployment's `.env` rather than of the tenant's configuration -- two
tenants on one instance could not use different providers, and a buyer could
not switch from Apollo to a CSV export without an engineer editing Python.

The interfaces here are `Protocol`s, not base classes, deliberately. The three
capabilities that already had an ABC (email sending, email reading, CRM) keep
it, and their existing concrete classes satisfy the matching Protocol without
inheriting anything new. Structural typing is what lets this layer be added
around working code instead of through it.

WHAT AN ADAPTER MAY AND MAY NOT DO

  * MAY wrap an existing `src/integrations/*` function. That is the intent --
    the retry policy, the free-tier counters, the quota logging and the
    politeness throttles in those modules are load-bearing and tested.
  * MAY return an empty result. "I found nothing" is an answer.
  * MUST NOT raise for a missing credential. `available()` reports that.
    A node asking for a provider that is not configured gets the null adapter
    for its capability, so a batch degrades instead of crashing.
  * MUST NOT log a credential, or put one in an exception message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #


class Capability(str, Enum):
    """
    A job the pipeline needs done, independent of who does it.

    These strings are the keys in the tenant config's `providers:` block and
    the `capability` field in the registry, so they are part of a persisted
    contract -- renaming one is a migration, not a rename.

    Discovery has two entries rather than one because the two are not
    interchangeable: a B2B contact search cannot find a dental clinic in
    Manchester, and a local-business search cannot find a Head of Support. A
    niche's `type` decides which one applies. Both are served by the single
    `DiscoveryProvider` interface below.
    """

    LLM = "llm"
    DISCOVERY_LOCAL = "discovery_local"
    DISCOVERY_B2B = "discovery_b2b"
    ENRICHMENT = "enrichment"
    EMAIL_SENDER = "email_sender"
    EMAIL_READER = "email_reader"
    CRM = "crm"
    TRANSLATION = "translation"

    @property
    def interface(self) -> str:
        """The Protocol every adapter for this capability implements."""
        return _INTERFACE_FOR[self]


_INTERFACE_FOR: dict[Capability, str] = {
    Capability.LLM: "LLMProvider",
    Capability.DISCOVERY_LOCAL: "DiscoveryProvider",
    Capability.DISCOVERY_B2B: "DiscoveryProvider",
    Capability.ENRICHMENT: "EnrichmentProvider",
    Capability.EMAIL_SENDER: "EmailSender",
    Capability.EMAIL_READER: "EmailReader",
    Capability.CRM: "CRMProvider",
    Capability.TRANSLATION: "TranslationProvider",
}

#: Which niche `type` maps to which discovery capability.
DISCOVERY_CAPABILITY_FOR_NICHE_TYPE: dict[str, Capability] = {
    "local_business": Capability.DISCOVERY_LOCAL,
    "b2b": Capability.DISCOVERY_B2B,
}


class CredentialType(str, Enum):
    """
    What a provider needs from the user before it can be used.

    Fixed by earlier decisions and not revisited here: no OAuth flows, because
    an app password the user pastes once is something they can revoke
    themselves and something this product can store without becoming an
    identity provider.
    """

    API_KEY = "api_key"
    EMAIL_PASSWORD = "email_password"
    SERVICE_ACCOUNT_JSON = "service_account_json"
    NONE = "none"
    #: The generic REST adapter: base URL, auth style, token, extra headers.
    CUSTOM_REST = "custom_rest"


# --------------------------------------------------------------------------- #
# Shared result types
# --------------------------------------------------------------------------- #


@dataclass
class DiscoveryRequest:
    """
    What to look for, in terms every discovery provider can honour.

    A local-business provider reads `search_terms` and `locations` and ignores
    the rest; a contact provider reads `titles`, `industries` and
    `employee_range`. Sending one request shape to both is what lets N1 stop
    branching on the vendor.
    """

    kind: str = "local_business"       # local_business | b2b
    search_terms: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    employee_range: list[int] | None = None
    limit: int = 20
    #: Filename glob for the CSV-import provider, from the niche config.
    csv_glob: str = ""
    #: For logging and the custom adapter's payload. Never a credential.
    niche_id: str = ""
    region: str = ""


@dataclass
class DiscoveredBusiness:
    """
    One business a provider found, in the shape N1 turns into a lead.

    This is a superset of what the local-business and contact providers each
    return, not a lowest common denominator: a local search fills in `address`,
    `rating` and `review_count` and leaves `title` empty, a contact search does
    the reverse. N1 builds its discovery-time signals from whichever fields
    came back, so a provider that supplies more produces a richer lead without
    N1 knowing which provider it was.
    """

    company_name: str
    website: str = ""
    address: str = ""
    phone: str = ""
    category: str = ""
    rating: float | None = None
    review_count: int | None = None
    business_status: str = ""
    contact_name: str = ""
    contact_email: str = ""
    title: str = ""
    linkedin_url: str = ""
    location: str = ""
    industry: str = ""
    employee_count: int | None = None
    #: The vendor that found it, recorded on the lead for auditing.
    source: str = ""

    @property
    def is_open(self) -> bool:
        """False only when a provider explicitly said the business has closed."""
        return self.business_status.upper() not in ("CLOSED_PERMANENTLY",)

    @property
    def where(self) -> str:
        """Whichever of address/location the provider filled in."""
        return self.address or self.location


@dataclass
class FoundEmail:
    """One address an enrichment provider found, with its own confidence."""

    email: str
    confidence: int = 0
    first_name: str = ""
    last_name: str = ""
    position: str = ""
    source: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(x for x in (self.first_name, self.last_name) if x)


@dataclass
class TranslatedText:
    """
    A translation, or an honest report that none happened.

    `translated=False` with the original text is a valid, non-exceptional
    result: sending readable English beats sending nothing, and N4 records the
    flag so a human reviewing the draft can see localisation did not run.
    """

    text: str
    language: str
    provider: str
    translated: bool


# --------------------------------------------------------------------------- #
# Capability interfaces
# --------------------------------------------------------------------------- #


@runtime_checkable
class CapabilityProvider(Protocol):
    """Common to all seven. `id` is the registry id, not a display name."""

    id: str

    def available(self) -> bool:
        """
        True when this provider has what it needs to make a real call.

        Checked before use so a missing credential becomes a logged skip
        instead of an exception halfway through a batch.
        """
        ...


@runtime_checkable
class LLMProvider(CapabilityProvider, Protocol):
    """Turns a prompt into text. Used by N3, N4 and N7."""

    def complete(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.4,
        context: dict | None = None,
    ) -> Any:
        """Returns an `llm.LLMResponse`. Raises `LLMUnavailable` if it cannot."""
        ...

    def complete_json(
        self,
        prompt: str,
        *,
        task: str,
        system: str = "",
        temperature: float = 0.2,
        context: dict | None = None,
        required_keys: tuple[str, ...] = (),
    ) -> tuple[dict, Any]:
        ...

    def model_name(self) -> str:
        ...


@runtime_checkable
class DiscoveryProvider(Protocol):
    """
    Finds businesses or contacts to reach out to. Used by N1.

    `kind` says which sort it finds, matching a niche's `type`. The resolver
    will not hand a `b2b` provider to a `local_business` niche.
    """

    id: str
    kind: str

    def available(self) -> bool:
        ...

    def find(self, request: DiscoveryRequest) -> list[DiscoveredBusiness]:
        """
        Never raises for an ordinary failure. A provider that is down, out of
        quota or misconfigured returns `[]` and logs why -- discovery producing
        fewer leads is a bad batch, discovery raising is a broken product.
        """
        ...


@runtime_checkable
class EnrichmentProvider(Protocol):
    """
    Turns a company domain into a named person with a real address. Used by N2.

    Website scraping is not a provider: it always runs, needs no credential and
    has nothing to swap. What is swappable is the paid lookup service that
    finds an address scraping could not.
    """

    id: str

    def available(self) -> bool:
        ...

    def find_email(
        self, domain: str, *, first_name: str = "", last_name: str = ""
    ) -> FoundEmail | None:
        """None for every "could not do this" case, including no quota left."""
        ...

    def budget_remaining(self) -> int:
        """
        Lookups left in the free allowance this period.

        N2 uses this to decide how many leads to spend a lookup on before it
        spends any, so asking for five when two remain cannot burn the two on
        the wrong leads. A provider with no metered allowance returns a large
        number rather than pretending to be exhausted.
        """
        ...

    def budget_status(self) -> str:
        """One short human-readable line for the batch log."""
        ...


@runtime_checkable
class EmailSender(Protocol):
    """
    Sends one email. Used by N6a.

    Satisfied as-is by the existing `integrations.email_sender.EmailSender`
    subclasses -- structural typing, no edit to those classes.
    """

    provider: str

    def check_budget(self) -> None:
        """Raises `DailyLimitReached` when today's cap is spent."""
        ...

    def send(self, *args: Any, **kwargs: Any) -> Any:
        ...


@runtime_checkable
class EmailReader(Protocol):
    """Reads replies. Used by N7. Satisfied by the existing reader classes."""

    def fetch_replies(self, address: str, since: datetime) -> list[Any]:
        ...


@runtime_checkable
class CRMProvider(Protocol):
    """Records the outcome. Used by N9. Satisfied by the existing backends."""

    def upsert_lead(self, state: Any) -> str:
        ...

    def append_suppression(self, entry: dict[str, Any]) -> None:
        ...


@runtime_checkable
class TranslationProvider(Protocol):
    """Localises a draft. Used by N4."""

    id: str

    def available(self) -> bool:
        ...

    def translate(self, text: str, language: str) -> TranslatedText | None:
        """
        None means "I could not, try the next provider" -- which is how the
        resolver's fallback chain knows to move on. A provider that succeeds
        returns `translated=True`; one that legitimately has nothing to do
        (the text is already in the target language) returns `translated=False`.
        """
        ...


# --------------------------------------------------------------------------- #
# What an adapter is built with
# --------------------------------------------------------------------------- #


@dataclass
class ProviderContext:
    """
    Everything an adapter needs that is not a credential.

    Credentials are never passed through here. An adapter reads them the same
    way the rest of the agent does -- `src.settings.credential()`, which checks
    the encrypted per-workspace store first and `.env` second -- so a key never
    travels through the resolver, never lands in a log line, and never sits in
    a `ProviderContext` that something might repr().

    `options` is the tenant's non-secret configuration for this capability:
    daily send limits, the CRM worksheet name, the LLM fallback chain, the
    custom endpoint's base URL. Whatever an adapter needs from the tenant's
    YAML arrives here rather than the adapter loading the config itself, so an
    adapter can be built in a test with a literal dict.
    """

    tenant_id: str = ""
    dry_run: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)
