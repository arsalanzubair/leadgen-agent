"""
src/providers -- the capability layer.

Nodes import from here and nowhere deeper. What each name is for:

    Capability            the eight jobs the pipeline needs done
    provider_for          tenant selection -> registry -> a live adapter
    llm_for, sender_for,  the per-capability shorthands the nodes actually use
    discovery_for, ...
    DiscoveryRequest      what to look for, in vendor-neutral terms
    DiscoveredBusiness    what came back, in vendor-neutral terms

`registry` is the single list of who can do what; the settings service imports
it directly to render the Connections screen and to answer
`GET /api/providers`. Nothing else should hardcode a vendor name.
"""

from src.providers.base import (
    Capability,
    CapabilityProvider,
    CredentialType,
    CRMProvider,
    DiscoveredBusiness,
    DiscoveryProvider,
    DiscoveryRequest,
    EmailReader,
    EmailSender,
    EnrichmentProvider,
    FoundEmail,
    LLMProvider,
    ProviderContext,
    TranslatedText,
    TranslationProvider,
)
from src.providers.resolve import (
    Selection,
    crm_for,
    describe,
    discovery_for,
    enrichment_for,
    llm_for,
    provider_for,
    reader_for,
    selection_for,
    sender_for,
    translation_for,
)

__all__ = [
    "Capability",
    "CapabilityProvider",
    "CredentialType",
    "CRMProvider",
    "DiscoveredBusiness",
    "DiscoveryProvider",
    "DiscoveryRequest",
    "EmailReader",
    "EmailSender",
    "EnrichmentProvider",
    "FoundEmail",
    "LLMProvider",
    "ProviderContext",
    "Selection",
    "TranslatedText",
    "TranslationProvider",
    "crm_for",
    "describe",
    "discovery_for",
    "enrichment_for",
    "llm_for",
    "provider_for",
    "reader_for",
    "selection_for",
    "sender_for",
    "translation_for",
]
