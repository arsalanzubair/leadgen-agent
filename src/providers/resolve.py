"""
resolve.py -- tenant selection -> registry -> adapter instance -> interface.

This is the only module a node needs. A node says "give me something that can
find local businesses for this workspace" and gets back an object satisfying
the matching interface; it never learns which vendor answered, and switching
that vendor is an edit to a YAML file.

RESOLUTION ORDER, for one capability:

  1. `providers.<capability>.primary` in the tenant's config, if it names a
     provider the registry knows and has built.
  2. The legacy single-provider field, where one exists -- `runtime.llm_provider`,
     `crm.backend`, `sending_identity.provider`. Configs written before this
     layer existed keep working untouched, which is the point: nobody has to
     migrate a file to get an unchanged pipeline.
  3. The registry's default for that capability.

FALLBACK, where a tenant set one, applies when the primary CANNOT RUN -- no
credential, no endpoint -- and not when it runs and finds nothing. That
distinction is deliberate. "Apollo returned two contacts, now also read the
CSV folder" would double-count against a limit and produce duplicate leads;
"Apollo has no key, use the CSV folder" is what a fallback is for. The one
exception is the model capability, where the fallback joins the retry chain
inside `integrations.llm` because a mid-batch 429 genuinely should move on to
the next provider.

WHAT HAPPENS WHEN NOTHING RESOLVES

Never an exception. Each capability has a null adapter that does the honest
nothing -- finds no businesses, finds no addresses, translates nothing, writes
the outreach to a file instead of sending it -- and says so in the batch log.
A workspace with nothing connected should produce a batch that reports it
found nobody, not a stack trace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.providers.base import (
    DISCOVERY_CAPABILITY_FOR_NICHE_TYPE,
    Capability,
    DiscoveryRequest,
    ProviderContext,
    TranslatedText,
)
from src.providers.registry import ProviderNotBuilt, UnknownProvider
from src.providers import registry
from src.reliability import log

if TYPE_CHECKING:  # pragma: no cover
    from src.nodes.n0_config_load import TenantConfig


# --------------------------------------------------------------------------- #
# Reading the tenant's choice
# --------------------------------------------------------------------------- #

#: capability -> where the choice lived before the `providers:` block existed.
#: Dotted, read out of the raw tenant config.
LEGACY_FIELDS: dict[Capability, str] = {
    Capability.LLM: "runtime.llm_provider",
    Capability.CRM: "crm.backend",
    Capability.EMAIL_SENDER: "sending_identity.provider",
}

#: capability -> the .env variable the old factory read, before any of this
#: existed. Consulted after the tenant's config and before the registry
#: default, so an install that has been running on `LLM_PROVIDER=gemini` in a
#: .env file keeps using Gemini after this upgrade instead of silently
#: reverting to the registry's first entry.
LEGACY_ENV_VARS: dict[Capability, str] = {
    Capability.LLM: "LLM_PROVIDER",
    Capability.CRM: "CRM_BACKEND",
    Capability.EMAIL_SENDER: "EMAIL_PROVIDER",
    Capability.EMAIL_READER: "EMAIL_READER",
}

#: Legacy values that no longer name a registry provider, and what they meant.
LEGACY_ALIASES: dict[str, str] = {
    "google_sheets": "google_sheets",
    "gmail": "gmail_smtp",          # `sending_identity.provider: gmail`
    "smtp": "gmail_smtp",
    "sheets": "google_sheets",
    "dry_run": "dry_run",
}


class Selection:
    """One capability's resolved configuration, before anything is built."""

    __slots__ = ("capability", "primary", "fallback", "options", "source")

    def __init__(
        self,
        capability: Capability,
        primary: str,
        fallback: str = "",
        options: dict[str, Any] | None = None,
        source: str = "default",
    ) -> None:
        self.capability = capability
        self.primary = primary
        self.fallback = fallback
        self.options = options or {}
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover
        tail = f" fallback={self.fallback}" if self.fallback else ""
        return f"<Selection {self.capability.value}={self.primary}{tail} via {self.source}>"


def _dig(data: Any, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _providers_block(config: TenantConfig | None) -> dict[str, Any]:
    if config is None:
        return {}
    block = getattr(config, "providers", None)
    if callable(block):
        block = block()
    return block if isinstance(block, dict) else {}


def _legacy_choice(config: TenantConfig | None, capability: Capability) -> str:
    """The pre-`providers:` field for this capability, normalised."""
    dotted = LEGACY_FIELDS.get(capability)
    if not dotted or config is None:
        return ""
    raw = _dig(getattr(config, "raw", {}) or {}, dotted)
    return _normalise(raw)


def _legacy_env_choice(capability: Capability) -> str:
    """The pre-`providers:` .env variable for this capability, normalised."""
    key = LEGACY_ENV_VARS.get(capability)
    if not key:
        return ""
    from src.settings import env

    return _normalise(env(key, ""))


def _normalise(raw: object) -> str:
    value = str(raw or "").strip().lower()
    return LEGACY_ALIASES.get(value, value) if value else ""


def selection_for(
    capability: Capability | str, config: TenantConfig | None = None
) -> Selection:
    """
    What this workspace has chosen for one capability.

    A configured provider that the registry does not know, or knows but has
    not built, is a warning and a fall through to the default -- never a hard
    failure. A typo in a YAML file should degrade a batch's provider choice,
    not stop the batch.
    """
    capability = Capability(capability) if isinstance(capability, str) else capability
    block = _providers_block(config).get(capability.value)
    entry = block if isinstance(block, dict) else {}

    options = entry.get("settings")
    options = dict(options) if isinstance(options, dict) else {}

    fallback = str(entry.get("fallback", "") or "").strip().lower()
    if fallback and not registry.is_selectable(fallback, capability):
        log.warning(
            "tenant config names %r as the fallback for %s, which is not a "
            "provider that can do that job; ignoring it",
            fallback, capability.value,
        )
        fallback = ""

    for candidate, source in (
        (str(entry.get("primary", "") or "").strip().lower(), "tenant config"),
        (_legacy_choice(config, capability), "legacy config field"),
        (_legacy_env_choice(capability), "legacy .env variable"),
    ):
        if not candidate:
            continue
        if registry.is_selectable(candidate, capability):
            return Selection(capability, candidate, fallback, options, source)
        log.warning(
            "tenant config names %r for %s (%s), which is not a provider that "
            "can do that job; using the default instead",
            candidate, capability.value, source,
        )

    return Selection(
        capability, registry.default_for(capability), fallback, options, "default"
    )


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def _context(
    config: TenantConfig | None,
    capability: Capability,
    selection: Selection,
    *,
    tenant_id: str,
    dry_run: bool,
    extra: dict[str, Any] | None,
) -> ProviderContext:
    """
    Assemble the non-secret options an adapter needs from the tenant's config.

    Done here rather than in each adapter so an adapter stays testable with a
    literal dict and never reaches for the config loader itself.
    """
    options: dict[str, Any] = dict(selection.options)

    if config is not None:
        if capability is Capability.EMAIL_SENDER:
            options.setdefault("daily_limits", dict(config.daily_send_limits))
        elif capability is Capability.CRM:
            crm = dict(config.crm or {})
            options.setdefault("worksheet_name", crm.get("worksheet_name", "Leads"))
            options.setdefault(
                "suppression_worksheet_name",
                crm.get("suppression_worksheet_name", "Suppression"),
            )

    if capability is Capability.LLM and selection.fallback:
        options.setdefault("fallback", selection.fallback)

    if extra:
        options.update(extra)

    return ProviderContext(
        tenant_id=tenant_id or getattr(config, "tenant_id", "") or "",
        dry_run=dry_run,
        options=options,
    )


def _build(spec: registry.ProviderSpec, ctx: ProviderContext) -> Any:
    factory = registry.load_factory(spec)
    return factory(spec, ctx)


def _is_available(adapter: Any) -> bool:
    """
    Whether an adapter reports it can make a real call.

    An adapter without `available()` -- the email senders, readers and CRM
    backends, which are the pre-existing classes -- counts as available: their
    own factories already degraded to a working fallback before returning, so
    second-guessing them here would replace a considered choice with a guess.
    """
    check = getattr(adapter, "available", None)
    if not callable(check):
        return True
    try:
        return bool(check())
    except Exception as exc:  # noqa: BLE001 - availability must never raise
        log.warning("availability check failed for %r: %s", adapter, exc)
        return False


def _has_credentials(spec: registry.ProviderSpec, tenant_id: str) -> bool:
    """
    Whether every required credential for this provider resolves.

    Reads through `src.settings.credential`, which checks the workspace's
    encrypted store first and `.env` second, and keeps only the boolean. The
    values themselves are not returned, logged or stored anywhere by this
    function.

    A provider with no required fields, or one whose fields map to no
    environment variable, is treated as satisfied: the registry is describing
    something that needs nothing, and demanding a credential it never declared
    would report a working provider as broken. The exception is a provider that
    declares `requires_any` -- alternative credential sets, any one of which
    will do -- which is how the IMAP reader says "my own login, or the one you
    already gave for sending".
    """
    from src.settings import credential

    def resolves(env_var: str) -> bool:
        return bool(credential(env_var, tenant_id, ""))

    if spec.requires_any:
        return any(
            all(resolves(env_var) for env_var in group)
            for group in spec.requires_any
        )

    required = [f.name for f in spec.connection_fields if f.required]
    wanted = [spec.env_vars.get(name, "") for name in required]
    wanted = [name for name in wanted if name]
    if not wanted:
        return True
    return all(resolves(name) for name in wanted)


def _can_run(spec: registry.ProviderSpec, adapter: Any, tenant_id: str) -> bool:
    """Whether the selected provider itself -- not a fallback -- can work."""
    if not spec.enabled:
        return False
    if spec.is_custom:
        return _is_available(adapter)
    if spec.credential_type is registry.CredentialType.NONE:
        return _is_available(adapter)
    if not _has_credentials(spec, tenant_id):
        return False
    return _is_available(adapter)


def provider_for(
    capability: Capability | str,
    config: TenantConfig | None = None,
    *,
    tenant_id: str = "",
    dry_run: bool = False,
    options: dict[str, Any] | None = None,
    require_available: bool = True,
) -> Any:
    """
    The adapter this workspace should use for one capability.

    `require_available=False` returns the primary regardless -- used by the
    settings service, which wants to describe what is configured rather than
    what would run right now.
    """
    capability = Capability(capability) if isinstance(capability, str) else capability
    selection = selection_for(capability, config)
    ctx = _context(
        config, capability, selection,
        tenant_id=tenant_id, dry_run=dry_run, extra=options,
    )

    candidates = [selection.primary]
    if selection.fallback and selection.fallback != selection.primary:
        candidates.append(selection.fallback)

    for provider_id in candidates:
        if not provider_id:
            continue
        try:
            spec = registry.get(provider_id)
            adapter = _build(spec, ctx)
        except (UnknownProvider, ProviderNotBuilt) as exc:
            log.warning("cannot use %r for %s: %s", provider_id, capability.value, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            log.error(
                "building %r for %s failed: %s: %s",
                provider_id, capability.value, exc.__class__.__name__, exc,
            )
            continue

        if not require_available or _is_available(adapter):
            log.debug("%s -> %s (%s)", capability.value, provider_id, selection.source)
            return adapter

        log.info(
            "%s is selected for %s but is not set up, so it cannot run",
            provider_id, capability.value,
        )

    return _null_for(capability, ctx)


# --------------------------------------------------------------------------- #
# Null adapters
# --------------------------------------------------------------------------- #


def _null_for(capability: Capability, ctx: ProviderContext) -> Any:
    """The honest nothing, per capability."""
    if capability in (Capability.DISCOVERY_LOCAL, Capability.DISCOVERY_B2B):
        from src.providers.discovery_adapters import NoDiscovery

        kind = "local_business" if capability is Capability.DISCOVERY_LOCAL else "b2b"
        return NoDiscovery(kind=kind, reason="nothing connected for this job")

    if capability is Capability.ENRICHMENT:
        from src.providers.enrichment_adapters import WebsiteOnlyEnrichment

        return WebsiteOnlyEnrichment()

    if capability is Capability.TRANSLATION:
        from src.providers.translation_adapters import NoTranslation

        return NoTranslation()

    if capability is Capability.EMAIL_SENDER:
        from src.integrations.email_sender import DryRunSender

        log.warning(
            "no working email provider for tenant=%s; drafts will be written to "
            "a file instead of sent", ctx.tenant_id,
        )
        return DryRunSender(ctx.tenant_id, None)

    if capability is Capability.EMAIL_READER:
        from src.integrations.email_reader import NullReader

        return NullReader()

    if capability is Capability.CRM:
        from src.integrations.sheets_crm import DryRunCRM

        return DryRunCRM(ctx.tenant_id)

    if capability is Capability.LLM:
        # The mock is the null model: deterministic canned answers rather than
        # an exception, so Test Mode works on a machine with no keys at all.
        from src.providers.llm_adapters import LLMAdapter

        return LLMAdapter("mock")

    raise RuntimeError(f"no null adapter for {capability.value}")


# --------------------------------------------------------------------------- #
# Per-capability convenience
# --------------------------------------------------------------------------- #


def llm_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """The model this workspace writes with. Used by N3, N4 and N7."""
    return provider_for(Capability.LLM, config, **kwargs)


def discovery_for(
    config: TenantConfig | None,
    niche: dict[str, Any] | None = None,
    *,
    niche_type: str = "",
    **kwargs: Any,
) -> Any:
    """
    The right discovery provider for one niche.

    Which of the two discovery capabilities applies is decided by the niche's
    `type`, not by the tenant: a local-business audience cannot be served by a
    contact database however the config is written. An unrecognised type is
    treated as local business, matching N1's own historical branch.
    """
    kind = str(niche_type or (niche or {}).get("type") or "local_business")
    capability = DISCOVERY_CAPABILITY_FOR_NICHE_TYPE.get(
        kind, Capability.DISCOVERY_LOCAL
    )
    return provider_for(capability, config, **kwargs)


def enrichment_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """The contact-lookup service. Used by N2."""
    return provider_for(Capability.ENRICHMENT, config, **kwargs)


def sender_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """The email sender. Used by N6a."""
    return provider_for(Capability.EMAIL_SENDER, config, **kwargs)


def reader_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """The reply reader. Used by N7."""
    return provider_for(Capability.EMAIL_READER, config, **kwargs)


def crm_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """The records backend. Used by N9."""
    return provider_for(Capability.CRM, config, **kwargs)


class TranslationChain:
    """
    The tenant's translation providers, in their order, then nothing.

    Translation is the one capability where trying the next provider on a
    FAILED call is right rather than double-counting: a DeepL quota error and
    a model retry produce one translation either way. So this wrapper exists
    and the other capabilities do not have one.
    """

    id = "chain"

    def __init__(self, providers: list[Any]) -> None:
        self._providers = providers

    def available(self) -> bool:
        return any(_is_available(p) for p in self._providers)

    def translate(self, text: str, language: str) -> TranslatedText | None:
        for provider in self._providers:
            if not _is_available(provider):
                continue
            result = provider.translate(text, language)
            if result is not None:
                return result
        return None


def translation_for(config: TenantConfig | None = None, **kwargs: Any) -> Any:
    """
    The translation chain for this workspace. Used by N4.

    Always returns something: a chain ending in the null translator, which
    hands back the English text with `translated=False` so the approval screen
    can show a human that a French lead is about to get an English email.
    """
    from src.providers.translation_adapters import NoTranslation

    selection = selection_for(Capability.TRANSLATION, config)
    ctx = _context(
        config, Capability.TRANSLATION, selection,
        tenant_id=kwargs.get("tenant_id", ""),
        dry_run=bool(kwargs.get("dry_run", False)),
        extra=kwargs.get("options"),
    )

    # The model sits behind whatever the workspace chose, unless it IS what
    # they chose or they named a different second link themselves. This is the
    # historical DeepL-then-model order, kept: a workspace with no DeepL key
    # should still get a translated draft rather than an English one.
    fallback = selection.fallback
    if not fallback and selection.primary != "llm":
        fallback = "llm"

    chain: list[Any] = []
    for provider_id in (selection.primary, fallback):
        if not provider_id or any(p.id == provider_id for p in chain):
            continue
        try:
            chain.append(_build(registry.get(provider_id), ctx))
        except (UnknownProvider, ProviderNotBuilt) as exc:
            log.warning("cannot use %r for translation: %s", provider_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.error("building %r for translation failed: %s", provider_id, exc)

    chain.append(NoTranslation())
    return TranslationChain(chain)


# --------------------------------------------------------------------------- #
# Description, for the settings service
# --------------------------------------------------------------------------- #


def describe(config: TenantConfig | None = None) -> dict[str, dict[str, Any]]:
    """
    What every capability is set to, and whether it can actually run.

    Read by `GET /api/providers/selection` so the Connections screen shows the
    workspace's real state rather than a hopeful default. Never includes a
    credential, and never includes the custom endpoint's token -- `settings`
    here is the non-secret block only.
    """
    out: dict[str, dict[str, Any]] = {}
    for capability in Capability:
        selection = selection_for(capability, config)
        tenant_id = getattr(config, "tenant_id", "") or ""
        spec = registry.PROVIDERS_BY_ID.get(selection.primary)
        try:
            adapter = provider_for(
                capability, config, require_available=False, tenant_id=tenant_id,
            )
            ready = spec is not None and _can_run(spec, adapter, tenant_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not describe %s: %s", capability.value, exc)
            ready = False

        out[capability.value] = {
            "capability": capability.value,
            "interface": capability.interface,
            "primary": selection.primary,
            "primary_name": spec.display_name if spec else selection.primary,
            "fallback": selection.fallback,
            "chosen_by": selection.source,
            "ready": ready,
            "settings": {
                key: value
                for key, value in selection.options.items()
                if key != "token"
            },
        }
    return out
