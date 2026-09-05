"""
N0 -- Tenant & ICP Config Load.

In:  tenant_id
Out: a validated TenantConfig, plus the resolved list of (niche_id, region)
     BatchTargets to process this batch.

Loads config/tenants/{tenant_id}.yaml and validates every required field
(ICPs, regions, channels, tone, sending_identity, compliance profile
reference). A missing required field HALTS the batch with a ConfigError naming
the field -- it never silently defaults, because a batch that quietly runs
against a default ICP or a default sending identity produces real emails to
real strangers with the wrong content.

Also loads config/compliance_profiles.yaml and config/cadences.yaml, merging
the tenant's `cadence_overrides` over the defaults, so downstream nodes call
one accessor instead of re-reading YAML.

The graph entry point (`n0_config_load`) is per-lead and idempotent: it
re-validates from the cache and attaches nothing to state. Batch-level callers
use `load_tenant_config()` / `resolve_batch_targets()` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.reliability import ConfigError, log, node
from src.settings import (
    CADENCES_PATH,
    COMPLIANCE_PROFILES_PATH,
    allowed_tenants,
    tenant_config_path,
)
from src.state import REGION_VALUES, BatchTarget, LeadState

# --------------------------------------------------------------------------- #
# Required-field contract
# --------------------------------------------------------------------------- #

#: Top-level keys that must be present and non-empty. Dotted paths descend into
#: nested mappings. Section 4, N0: "never silently default".
REQUIRED_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "regions",
    "niches",
    "channels.enabled",
    "tone",
    "sending_identity.from_name",
    "sending_identity.from_email",
    "sending_identity.company_name",
    "sending_identity.linkedin_account_label",
    "compliance.profile_set",
    "fit_score_threshold",
    "daily_send_limits",
    "language_map",
)

#: Required inside every entry of `niches:`.
#:
#: `channel_default` is deliberately NOT required. It is validated when present
#: (see below) and N3.5 falls back to email without it, so a config written
#: before the field existed still loads -- an existing install should not break
#: on an upgrade over a field it can safely default.
REQUIRED_NICHE_FIELDS: tuple[str, ...] = ("id", "type", "icp", "discovery")

#: Required inside every niche's `icp:`.
REQUIRED_ICP_FIELDS: tuple[str, ...] = ("description", "good_signals")

VALID_NICHE_TYPES = frozenset({"local_business", "b2b"})
VALID_CHANNELS = frozenset({"email", "linkedin"})

#: Valid values for a niche's `channel_default`. `both` is allowed here even
#: though `channels.enabled` only accepts the two real channels: a niche saying
#: "reach these people either way" is meaningful, and N3.5 narrows it down to
#: what the tenant has enabled and what contact data actually exists.
VALID_NICHE_CHANNEL_DEFAULTS = frozenset({"email", "linkedin", "both"})


# --------------------------------------------------------------------------- #
# Resolved config object
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TenantConfig:
    """
    A validated tenant configuration plus the compliance and cadence data the
    rest of the graph reads. Frozen because nodes must not mutate shared config
    -- a node that "just tweaks" the threshold would silently change every
    other lead in the batch.
    """

    tenant_id: str
    raw: dict[str, Any]
    compliance_profiles: dict[str, dict[str, Any]] = field(repr=False, default_factory=dict)
    cadences: dict[str, Any] = field(repr=False, default_factory=dict)
    cadence_meta: dict[str, Any] = field(repr=False, default_factory=dict)
    source_path: Path | None = field(repr=False, default=None)

    # -- simple accessors --------------------------------------------------- #

    @property
    def regions(self) -> list[str]:
        return list(self.raw["regions"])

    @property
    def niches(self) -> list[dict[str, Any]]:
        return list(self.raw["niches"])

    @property
    def niche_ids(self) -> list[str]:
        return [n["id"] for n in self.niches]

    @property
    def enabled_channels(self) -> list[str]:
        return list(self.raw["channels"]["enabled"])

    @property
    def preference_when_both(self) -> str:
        return self.raw.get("channels", {}).get("preference_when_both", "both")

    @property
    def tone(self) -> str:
        return self.raw["tone"]

    @property
    def sending_identity(self) -> dict[str, Any]:
        return self.raw["sending_identity"]

    @property
    def providers(self) -> dict[str, Any]:
        """
        Which provider serves each capability, for `src/providers/resolve.py`.

        Absent from a config written before the capability layer existed, which
        is why this returns an empty mapping rather than raising: the resolver
        then falls back to the legacy single-provider fields
        (`runtime.llm_provider`, `crm.backend`, `sending_identity.provider`)
        and, failing those, to the registry's own default. Nobody has to
        migrate a file to keep an unchanged pipeline.
        """
        block = self.raw.get("providers")
        return dict(block) if isinstance(block, dict) else {}

    @property
    def fit_score_threshold(self) -> int:
        return int(self.raw["fit_score_threshold"])

    @property
    def hunter_top_n(self) -> int:
        return int(self.raw.get("hunter_top_n_per_batch", 5))

    @property
    def max_leads_per_run(self) -> int:
        return int(self.raw.get("max_leads_per_run", 40))

    @property
    def daily_send_limits(self) -> dict[str, int]:
        return dict(self.raw["daily_send_limits"])

    @property
    def personalization(self) -> dict[str, Any]:
        return self.raw.get("personalization", {})

    @property
    def runtime(self) -> dict[str, Any]:
        return self.raw.get("runtime", {})

    @property
    def crm(self) -> dict[str, Any]:
        return self.raw.get("crm", {"backend": "google_sheets", "worksheet_name": "Leads"})

    @property
    def blocked_domains(self) -> set[str]:
        return {
            d.strip().lower()
            for d in self.raw.get("compliance", {}).get("blocked_domains", [])
            if d and d.strip()
        }

    @property
    def blocked_emails(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.raw.get("compliance", {}).get("blocked_emails", [])
            if e and e.strip()
        }

    @property
    def requires_approval(self) -> bool:
        return bool(self.raw.get("compliance", {}).get("require_approval_before_send", True))

    # -- lookups ------------------------------------------------------------ #

    def language_for(self, region: str) -> str:
        return self.raw.get("language_map", {}).get(region, "en")

    def niche(self, niche_id: str) -> dict[str, Any]:
        for entry in self.niches:
            if entry["id"] == niche_id:
                return entry
        raise ConfigError(
            f"tenant '{self.tenant_id}': unknown niche_id '{niche_id}'. "
            f"Configured niches: {self.niche_ids}"
        )

    def compliance_profile(self, region: str) -> dict[str, Any]:
        """
        The region's rules, with the file's `defaults:` block underneath. N5.5
        can therefore read any flag without worrying whether that region
        happened to declare it.
        """
        if region not in self.compliance_profiles:
            raise ConfigError(
                f"no compliance profile for region '{region}' in "
                f"{COMPLIANCE_PROFILES_PATH.name}. Add one before running this region."
            )
        return self.compliance_profiles[region]

    def cadence_for(self, channel: str) -> dict[str, Any]:
        """Cadence for a channel, with this tenant's overrides already merged."""
        if channel not in self.cadences:
            raise ConfigError(
                f"no cadence defined for channel '{channel}' in {CADENCES_PATH.name}"
            )
        return self.cadences[channel]

    def max_touches(self, channel: str, region: str) -> int:
        """
        Effective touch ceiling: the shorter of the cadence and the region's
        compliance cap. An EU lead on a 3-touch cadence stops after 2.
        """
        cadence_len = len(self.cadence_for(channel).get("steps", []))
        region_cap = int(
            self.compliance_profile(region).get("max_touches_without_reply", cadence_len)
        )
        return max(1, min(cadence_len, region_cap))

    def on_reply_rule(self, reply_category: str) -> dict[str, Any]:
        return self.cadence_meta.get("on_reply", {}).get(reply_category, {})

    @property
    def sending_windows(self) -> dict[str, Any]:
        return self.cadence_meta.get("sending_windows", {})


# --------------------------------------------------------------------------- #
# Loading + validation
# --------------------------------------------------------------------------- #

def _read_yaml(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"{what} not found at {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{what} at {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{what} at {path} must be a YAML mapping, got {type(data).__name__}")
    return data


def _dig(data: dict[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _validate(raw: dict[str, Any], tenant_id: str, path: Path) -> None:
    """
    Collect EVERY problem before raising, so the operator fixes the config in
    one pass instead of rerunning to discover the next missing field.
    """
    problems: list[str] = []

    for dotted in REQUIRED_FIELDS:
        value = _dig(raw, dotted)
        if value is None or (isinstance(value, (str, list, dict)) and len(value) == 0):
            problems.append(f"missing required field: {dotted}")

    declared_id = raw.get("tenant_id")
    if declared_id and declared_id != tenant_id:
        problems.append(
            f"tenant_id mismatch: file is {path.name} but declares "
            f"tenant_id: {declared_id!r}. The filename and the id must match, "
            "because every per-tenant resource is resolved from the id."
        )

    # -- regions ------------------------------------------------------------ #
    regions = raw.get("regions") or []
    for region in regions:
        if region not in REGION_VALUES:
            problems.append(
                f"regions: {region!r} is not a supported region "
                f"{sorted(REGION_VALUES)}"
            )

    # -- language_map covers every region ----------------------------------- #
    language_map = raw.get("language_map") or {}
    for region in regions:
        if region not in language_map:
            problems.append(f"language_map is missing an entry for region {region!r}")

    # -- channels ----------------------------------------------------------- #
    enabled = _dig(raw, "channels.enabled") or []
    for channel in enabled:
        if channel not in VALID_CHANNELS:
            problems.append(
                f"channels.enabled: {channel!r} is not one of {sorted(VALID_CHANNELS)}"
            )
    preference = _dig(raw, "channels.preference_when_both")
    if preference is not None and preference not in ("email", "linkedin", "both"):
        problems.append(
            f"channels.preference_when_both: {preference!r} must be "
            "'email', 'linkedin' or 'both'"
        )

    # -- niches ------------------------------------------------------------- #
    niches = raw.get("niches") or []
    seen_ids: set[str] = set()
    for index, niche in enumerate(niches):
        where = f"niches[{index}]"
        if not isinstance(niche, dict):
            problems.append(f"{where} must be a mapping")
            continue
        for key in REQUIRED_NICHE_FIELDS:
            if not niche.get(key):
                problems.append(f"{where}: missing required field '{key}'")
        niche_id = niche.get("id")
        if niche_id:
            where = f"niches[{niche_id}]"
            if niche_id in seen_ids:
                problems.append(f"duplicate niche id {niche_id!r}")
            seen_ids.add(niche_id)
        niche_type = niche.get("type")
        if niche_type and niche_type not in VALID_NICHE_TYPES:
            problems.append(
                f"{where}: type {niche_type!r} must be one of {sorted(VALID_NICHE_TYPES)}"
            )
        channel_default = niche.get("channel_default")
        if channel_default is not None and channel_default not in VALID_NICHE_CHANNEL_DEFAULTS:
            problems.append(
                f"{where}: channel_default {channel_default!r} must be one of "
                f"{sorted(VALID_NICHE_CHANNEL_DEFAULTS)}"
            )

        icp = niche.get("icp") or {}
        if isinstance(icp, dict):
            for key in REQUIRED_ICP_FIELDS:
                if not icp.get(key):
                    problems.append(f"{where}.icp: missing required field '{key}'")
        else:
            problems.append(f"{where}.icp must be a mapping")
        discovery = niche.get("discovery") or {}
        if isinstance(discovery, dict):
            if niche_type == "local_business" and not discovery.get("search_terms"):
                problems.append(
                    f"{where}.discovery: local_business niches need 'search_terms'"
                )
            if niche_type == "b2b" and not discovery.get("titles"):
                problems.append(
                    f"{where}.discovery: b2b niches need 'titles' for role-based targeting"
                )
            if not discovery.get("locations"):
                problems.append(f"{where}.discovery: missing 'locations'")
        else:
            problems.append(f"{where}.discovery must be a mapping")

    # -- thresholds --------------------------------------------------------- #
    threshold = raw.get("fit_score_threshold")
    if threshold is not None:
        if not isinstance(threshold, int) or isinstance(threshold, bool):
            problems.append("fit_score_threshold must be an integer")
        elif not 0 <= threshold <= 100:
            problems.append(f"fit_score_threshold {threshold} must be between 0 and 100")

    limits = raw.get("daily_send_limits") or {}
    if isinstance(limits, dict):
        for key, value in limits.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(
                    f"daily_send_limits.{key} must be a non-negative integer, got {value!r}"
                )
    else:
        problems.append("daily_send_limits must be a mapping")

    # -- provider selection ------------------------------------------------- #
    #
    # Validated the same way `channel_default` is: only when present, and
    # against the registry rather than a list duplicated here. A workspace that
    # names a provider which cannot do the job it is assigned to would
    # otherwise fail at the node that needed it, halfway into a batch, with a
    # message about an adapter rather than about their configuration.
    providers_block = raw.get("providers")
    if providers_block is not None:
        if not isinstance(providers_block, dict):
            problems.append("providers must be a mapping of capability -> choice")
        else:
            from src.providers import registry as _registry
            from src.providers.base import Capability as _Capability

            known = {c.value for c in _Capability}
            for capability, entry in providers_block.items():
                where = f"providers.{capability}"
                if capability not in known:
                    problems.append(
                        f"{where}: {capability!r} is not a capability "
                        f"{sorted(known)}"
                    )
                    continue
                if not isinstance(entry, dict):
                    problems.append(
                        f"{where} must be a mapping with a 'primary' key"
                    )
                    continue

                allowed = _registry.selectable_ids(capability)
                for slot in ("primary", "fallback"):
                    chosen = entry.get(slot)
                    if chosen in (None, ""):
                        continue
                    if not isinstance(chosen, str):
                        problems.append(f"{where}.{slot} must be a provider id")
                    elif chosen not in allowed:
                        problems.append(
                            f"{where}.{slot}: {chosen!r} cannot do that job. "
                            f"Choose one of {allowed}"
                        )

                settings = entry.get("settings")
                if settings is not None and not isinstance(settings, dict):
                    problems.append(f"{where}.settings must be a mapping")
                elif isinstance(settings, dict) and settings.get("token"):
                    # A token in the YAML would be a plaintext credential in a
                    # file that gets copied, diffed and shared. The encrypted
                    # store is the only place one belongs.
                    problems.append(
                        f"{where}.settings must not contain 'token'. Save the "
                        "key through Settings, Connections instead; it is "
                        "encrypted there and never written to this file."
                    )

    # -- sending identity sanity ------------------------------------------- #
    from_email = _dig(raw, "sending_identity.from_email")
    if from_email and "@" not in str(from_email):
        problems.append(f"sending_identity.from_email {from_email!r} is not an email address")

    if problems:
        listing = "\n  - ".join(problems)
        raise ConfigError(
            f"tenant config {path} is invalid; halting before Discovery.\n"
            f"  - {listing}"
        )


def _merge_cadences(
    defaults: dict[str, Any], overrides: dict[str, Any]
) -> dict[str, Any]:
    """
    Merge a tenant's cadence_overrides over the defaults.

    Steps are merged BY `step` number rather than replaced wholesale, so a
    tenant that only wants a different wait_days on touch 2 does not have to
    restate the whole cadence (and cannot accidentally drop the intent text
    that N4 relies on).
    """
    merged = {name: dict(cadence) for name, cadence in defaults.items()}
    for channel, override in (overrides or {}).items():
        base = dict(merged.get(channel, {}))
        override = dict(override or {})
        override_steps = override.pop("steps", None)
        base.update(override)
        if override_steps is not None:
            by_number = {int(s["step"]): dict(s) for s in base.get("steps", [])}
            for step in override_steps:
                number = int(step["step"])
                by_number.setdefault(number, {}).update(step)
            base["steps"] = [by_number[k] for k in sorted(by_number)]
        merged[channel] = base
    return merged


@lru_cache(maxsize=32)
def load_tenant_config(tenant_id: str) -> TenantConfig:
    """
    Load, validate and cache a tenant's configuration.

    Cached because every node needs it for every lead and re-parsing three YAML
    files per lead per node is pure waste. `load_tenant_config.cache_clear()`
    is available to tests and to a long-running scheduler that wants to pick up
    an edited config.

    Raises ConfigError -- deliberately not swallowed by @node.
    """
    allowed = allowed_tenants()
    if allowed and tenant_id not in allowed:
        raise ConfigError(
            f"tenant '{tenant_id}' is not in LEADGEN_TENANTS ({sorted(allowed)}). "
            "Add it there before running, so a typo cannot start a batch against "
            "an unintended tenant."
        )

    path = tenant_config_path(tenant_id)
    if not path.exists():
        raise ConfigError(
            f"no config for tenant '{tenant_id}': expected {path}. "
            "Copy config/tenants/example_tenant.yaml and edit it."
        )

    raw = _read_yaml(path, f"tenant config for '{tenant_id}'")
    _validate(raw, tenant_id, path)

    compliance_doc = _read_yaml(COMPLIANCE_PROFILES_PATH, "compliance profiles")
    compliance_defaults = compliance_doc.get("defaults", {}) or {}
    profiles: dict[str, dict[str, Any]] = {}
    for region, profile in (compliance_doc.get("regions") or {}).items():
        merged = dict(compliance_defaults)
        merged.update(profile or {})
        profiles[region] = merged

    missing_profiles = [r for r in raw.get("regions", []) if r not in profiles]
    if missing_profiles:
        raise ConfigError(
            f"tenant '{tenant_id}' targets regions {missing_profiles} but "
            f"{COMPLIANCE_PROFILES_PATH.name} has no profile for them. "
            "A region without a compliance profile must not be sent to."
        )

    cadence_doc = _read_yaml(CADENCES_PATH, "cadences")
    cadences = _merge_cadences(
        cadence_doc.get("cadences", {}) or {}, raw.get("cadence_overrides", {}) or {}
    )
    cadence_meta = {
        "on_reply": cadence_doc.get("on_reply", {}) or {},
        "sending_windows": cadence_doc.get("sending_windows", {}) or {},
    }

    log.info(
        "loaded tenant config tenant=%s niches=%s regions=%s channels=%s",
        tenant_id, [n["id"] for n in raw["niches"]], raw["regions"],
        raw["channels"]["enabled"],
    )
    return TenantConfig(
        tenant_id=tenant_id,
        raw=raw,
        compliance_profiles=profiles,
        cadences=cadences,
        cadence_meta=cadence_meta,
        source_path=path,
    )


def resolve_batch_targets(config: TenantConfig) -> list[BatchTarget]:
    """
    The (niche_id, region) pairs to process this batch.

    A pair is only produced when the niche actually declares locations for that
    region -- a tenant operating in six regions but with dental clinics
    configured only for four should not generate two empty discovery calls and
    two spurious `low_yield` warnings.
    """
    targets: list[BatchTarget] = []
    for niche in config.niches:
        locations = (niche.get("discovery") or {}).get("locations") or {}
        for region in config.regions:
            if not locations.get(region):
                log.debug(
                    "skipping target tenant=%s niche=%s region=%s (no locations configured)",
                    config.tenant_id, niche["id"], region,
                )
                continue
            targets.append(
                BatchTarget(
                    niche_id=niche["id"],
                    region=region,          # type: ignore[typeddict-item]
                    language=config.language_for(region),
                )
            )
    if not targets:
        raise ConfigError(
            f"tenant '{config.tenant_id}' resolved zero (niche, region) targets. "
            "Every niche's discovery.locations is empty for every configured "
            "region -- there is nothing to discover."
        )
    log.info(
        "resolved %d batch target(s) for tenant=%s: %s",
        len(targets), config.tenant_id,
        [f"{t['niche_id']}/{t['region']}" for t in targets],
    )
    return targets


# --------------------------------------------------------------------------- #
# Graph node
# --------------------------------------------------------------------------- #

@node("n0_config_load")
def n0_config_load(state: LeadState) -> dict:
    """
    Per-lead graph entry point.

    Batch-level discovery has already run by the time a lead enters the graph
    (see cli/run_batch.py), so this node's job here is to (a) re-assert that
    the tenant config is loadable and valid before anything touches the outside
    world, and (b) fill in the lead's `language` from the tenant's language_map
    if discovery did not set it.

    ConfigError propagates: a bad config halts, it does not degrade.
    """
    tenant_id = state.get("tenant_id")
    if not tenant_id:
        raise ConfigError("lead has no tenant_id; cannot resolve any tenant resource")

    config = load_tenant_config(tenant_id)

    niche_id = state.get("niche_id")
    if niche_id:
        config.niche(niche_id)          # raises ConfigError on an unknown niche

    region = state.get("region")
    if region and region not in config.regions:
        raise ConfigError(
            f"lead {state.get('lead_id')} has region {region!r}, which tenant "
            f"'{tenant_id}' does not operate in ({config.regions})"
        )

    updates: dict[str, Any] = {}
    if region and not state.get("language"):
        updates["language"] = config.language_for(region)
    return updates
