"""
workspace.py -- reading and writing the workspace's own configuration file.

This is an EDITOR for `config/tenants/<id>.yaml`, not a second store. Every
change made in Settings lands in that file, and the agent picks it up on its
next run through the same loader it always used. There is no shadow copy of the
user's configuration anywhere.

Two things this module is careful about:

  * Comments survive. The tenant config is heavily commented, and those
    comments are the documentation for anybody who opens the file by hand.
    Writes go through ruamel's round-trip loader so a save from the UI does not
    silently strip them. (PyYAML's `safe_dump` would.)

  * Writes are atomic, and validated first. The new document is checked with
    the agent's OWN validator before it replaces the old one, so the UI cannot
    write a config that would halt the next run. A rejected save leaves the
    file exactly as it was.

Placeholders: the shipped config has template values in the identity fields
rather than a real person's name. `_is_placeholder` recognises them, and the
profile endpoint reports them as empty -- so a fresh install shows a genuinely
blank profile and its prompts, instead of a fabricated sender that could end up
on real mail.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


from src.nodes.n0_config_load import _validate, load_tenant_config
from src.reliability import ConfigError
from src.settings import ROOT, allowed_tenants, env, tenant_config_path

def _round_trip_yaml() -> YAML:
    """
    A FRESH parser for every call. This is not fastidiousness.

    ruamel's `YAML` object carries mutable scanner and composer state, and it
    is not thread-safe. FastAPI runs sync endpoints in a threadpool, so the six
    parallel reads a single page load makes were sharing one parser and
    corrupting each other -- surfacing as intermittent `IndexError: string
    index out of range` and "expected a single document in the stream" on
    whichever request lost the race, on a file that was perfectly valid.

    Intermittent, load-dependent, and invisible in any single-request test:
    exactly the kind of bug that ships. Constructing the object is cheap;
    sharing it is not worth it.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096       # do not re-wrap the long description strings
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Which workspace this deployment is
# --------------------------------------------------------------------------- #

def resolve_tenant_id() -> str:
    """
    The one workspace this deployment serves.

    Resolution order, most explicit first:
      1. LEADGEN_WORKSPACE, if set.
      2. The first entry in LEADGEN_TENANTS, which the agent already uses to
         restrict what may run.
      3. The only config file in config/tenants/, if there is exactly one.
      4. "example_tenant", the shipped default.

    The user is never asked. This is plumbing.
    """
    explicit = env("LEADGEN_WORKSPACE")
    if explicit:
        return explicit

    allowed = sorted(allowed_tenants())
    if allowed:
        return allowed[0]

    directory = ROOT / "config" / "tenants"
    if directory.exists():
        files = sorted(p.stem for p in directory.glob("*.yaml"))
        if len(files) == 1:
            return files[0]

    return "example_tenant"


# --------------------------------------------------------------------------- #
# Reading and writing the file
# --------------------------------------------------------------------------- #

def config_path(tenant_id: str) -> Path:
    return tenant_config_path(tenant_id)


def read_config(tenant_id: str) -> Any:
    """The raw document, as a round-trip mapping that remembers its comments."""
    path = config_path(tenant_id)
    if not path.exists():
        raise ConfigError(
            f"no configuration file for this workspace: expected {path}. "
            "Copy config/tenants/example_tenant.yaml and name it after the "
            "workspace id."
        )
    # Read the whole file first, then parse the string. Parsing the handle
    # directly means a torn read -- another process mid-write, an editor
    # saving -- surfaces as ruamel's own `IndexError: string index out of
    # range`, which tells the operator nothing about which file is at fault.
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        # ruamel needs a final newline and raises an opaque IndexError without
        # one. A hand-edited file saved without a trailing newline is common
        # enough to be worth absorbing rather than reporting.
        text += "\n"
    try:
        document = _round_trip_yaml().load(text)
    except Exception as exc:      # noqa: BLE001 - ruamel raises several types
        raise ConfigError(
            f"{path} could not be read as YAML: {exc.__class__.__name__}: {exc}. "
            "The file has not been modified."
        ) from exc
    if document is None:
        raise ConfigError(f"{path} is empty")
    return document


def write_config(tenant_id: str, document: Any) -> None:
    """
    Validate, then replace the file atomically.

    The validator is the agent's own `_validate`, deliberately: if the UI could
    write a document the agent rejects, the failure would surface as a halted
    batch hours later rather than as a message on the screen of the person who
    caused it.
    """
    path = config_path(tenant_id)

    # ruamel's mappings are dict subclasses, so the validator reads them as-is.
    _validate(document, tenant_id, path)

    temporary = path.with_suffix(".yaml.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        _round_trip_yaml().dump(document, handle)
    os.replace(temporary, path)

    # The agent caches parsed configs; drop that so the next run sees the edit.
    load_tenant_config.cache_clear()


# --------------------------------------------------------------------------- #
# Placeholders
# --------------------------------------------------------------------------- #

#: A bracketed template value, e.g. "[set your name in Settings]".
_BRACKETED = re.compile(r"^\s*\[.*\]\s*$")

#: RFC 2606 reserves .invalid, so an address there can never be real.
_PLACEHOLDER_DOMAINS = ("example.invalid", "example.com", "example.org")


def _is_placeholder(value: Any) -> bool:
    """
    True for a shipped template value that should read as "not set yet".

    Without this, a fresh install would show template text as though the user
    had typed it, and the empty-state prompts would never appear.
    """
    # A key that is simply absent from the file is not set yet either. Without
    # this, str(None) reaches the client as the literal text "None" and lands
    # in a form field the user then has to delete.
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return True
    if _BRACKETED.match(text):
        return True
    return any(text.lower().endswith(domain) for domain in _PLACEHOLDER_DOMAINS)


def _clean(value: Any) -> str:
    """A stored value, or "" when it is blank or a template placeholder."""
    return "" if _is_placeholder(value) else str(value).strip()


# --------------------------------------------------------------------------- #
# Business profile
# --------------------------------------------------------------------------- #

def read_profile(tenant_id: str) -> dict[str, str]:
    """
    The profile, assembled from the two places it lives in the config.

    The sending identity has to stay in `sending_identity` because that is
    where the agent's compliance checks read it from. The descriptive fields
    are new, so they get their own block.
    """
    document = read_config(tenant_id)
    identity = document.get("sending_identity") or {}
    about = document.get("business_profile") or {}

    return {
        "business_name": _clean(identity.get("company_name")),
        "website": _clean(identity.get("website")),
        "what_you_sell": _clean(about.get("what_you_sell")),
        "who_you_serve": _clean(about.get("who_you_serve")),
        "problems_you_solve": _clean(about.get("problems_you_solve")),
        "what_makes_you_different": _clean(about.get("what_makes_you_different")),
        "services": _clean(about.get("services")),
        "target_markets": _clean(about.get("target_markets")),
        "tone_of_voice": _clean(about.get("tone_of_voice")),
        "sender_name": _clean(identity.get("from_name")),
        "sender_email": _clean(identity.get("from_email")),
        "postal_address": _clean(identity.get("physical_address")),
        "linkedin_account": _clean(identity.get("linkedin_account_label")),
    }


#: profile field -> where it lives in `sending_identity`.
_IDENTITY_KEYS = {
    "business_name": "company_name",
    "website": "website",
    "sender_name": "from_name",
    "sender_email": "from_email",
    "postal_address": "physical_address",
    "linkedin_account": "linkedin_account_label",
}

#: profile field -> where it lives in `business_profile`.
_ABOUT_KEYS = (
    "what_you_sell",
    "who_you_serve",
    "problems_you_solve",
    "what_makes_you_different",
    # Free text, and deliberately not the same thing as the audiences in
    # Targeting: this is how the user describes their own business, which is
    # what the drafter reads. Targeting is who to go looking for.
    "services",
    "target_markets",
    "tone_of_voice",
)


def save_profile(tenant_id: str, patch: dict[str, Any]) -> dict[str, str]:
    """Write the changed fields, leaving everything else in the file alone."""
    document = read_config(tenant_id)

    identity = document.setdefault("sending_identity", {})
    about = document.setdefault("business_profile", {})

    for profile_key, identity_key in _IDENTITY_KEYS.items():
        if profile_key in patch:
            value = str(patch[profile_key] or "").strip()
            # The agent's validator requires these to be non-empty, so clearing
            # a field puts the template placeholder back rather than emptying
            # it -- which is also what makes the UI show its prompt again.
            identity[identity_key] = value or _placeholder_for(identity_key)

    for key in _ABOUT_KEYS:
        if key in patch:
            about[key] = str(patch[key] or "").strip()

    # The tone the agent actually drafts with lives at the top level, so keep
    # it in step with what the user typed rather than leaving two versions.
    if patch.get("tone_of_voice"):
        document["tone"] = str(patch["tone_of_voice"]).strip()

    write_config(tenant_id, document)
    return read_profile(tenant_id)


def _placeholder_for(identity_key: str) -> str:
    return {
        "company_name": "[your business name - set in Settings]",
        "website": "[your website - set in Settings]",
        "from_name": "[your name - set in Settings]",
        "from_email": "set-your-sending-address@example.invalid",
        "physical_address": "[your postal address - set in Settings]",
        "linkedin_account_label": "[your LinkedIn account - set in Settings]",
    }.get(identity_key, "[set in Settings]")


# --------------------------------------------------------------------------- #
# Audiences
# --------------------------------------------------------------------------- #

def _niche_to_api(raw: dict[str, Any]) -> dict[str, Any]:
    icp = raw.get("icp") or {}
    discovery = raw.get("discovery") or {}
    return {
        "id": raw.get("id", ""),
        "label": raw.get("label") or raw.get("id", ""),
        "kind": raw.get("type", "local_business"),
        "search_terms": list(discovery.get("search_terms") or []),
        "titles": list(discovery.get("titles") or []),
        "locations": {
            region: list(places or [])
            for region, places in (discovery.get("locations") or {}).items()
        },
        "description": str(icp.get("description") or "").strip(),
        "must_have": list(icp.get("must_have") or []),
        "good_signals": list(icp.get("good_signals") or []),
        "disqualifiers": list(icp.get("disqualifiers") or []),
        # Explicit rather than guessed from the id. See the note in
        # src/nodes/n3_5_channel_selection.py.
        "channel_default": raw.get("channel_default", "email"),
        "tone": str(raw.get("tone") or "").strip(),
        "size_hint": str(icp.get("size_hint") or "").strip(),
    }


def _niche_from_api(payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload.get("kind", "local_business")
    discovery: dict[str, Any] = {
        "locations": {
            region: list(places)
            for region, places in (payload.get("locations") or {}).items()
            if places
        },
        "max_results_per_location": 20,
        "min_expected_leads": 5,
    }
    if kind == "b2b":
        discovery["titles"] = list(payload.get("titles") or [])
    else:
        discovery["search_terms"] = list(payload.get("search_terms") or [])

    return {
        "id": payload["id"],
        "label": payload.get("label") or payload["id"],
        "type": kind,
        "channel_default": payload.get("channel_default", "email"),
        "discovery": discovery,
        "icp": {
            "description": payload.get("description", ""),
            "must_have": list(payload.get("must_have") or []),
            "good_signals": list(payload.get("good_signals") or []),
            "disqualifiers": list(payload.get("disqualifiers") or []),
            "size_hint": payload.get("size_hint", ""),
        },
        "tone": payload.get("tone", ""),
    }


def read_niches(tenant_id: str) -> list[dict[str, Any]]:
    document = read_config(tenant_id)
    return [_niche_to_api(niche) for niche in (document.get("niches") or [])]


def save_niche(tenant_id: str, payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    document = read_config(tenant_id)
    niches = document.setdefault("niches", [])
    existing = {niche.get("id"): index for index, niche in enumerate(niches)}

    niche_id = payload["id"]
    if creating and niche_id in existing:
        raise ConfigError(
            f"there is already an audience called '{niche_id}'. Give this one a "
            "different name."
        )
    if not creating and niche_id not in existing:
        raise ConfigError(f"no audience called '{niche_id}' to update")

    entry = _niche_from_api(payload)
    if creating:
        niches.append(entry)
    else:
        # Merge, so anything in the file this UI does not know about survives.
        current = dict(niches[existing[niche_id]])
        current.update(entry)
        niches[existing[niche_id]] = current

    write_config(tenant_id, document)
    return _niche_to_api(entry)


def delete_niche(tenant_id: str, niche_id: str) -> None:
    document = read_config(tenant_id)
    niches = document.get("niches") or []
    remaining = [niche for niche in niches if niche.get("id") != niche_id]
    if len(remaining) == len(niches):
        raise ConfigError(f"no audience called '{niche_id}'")
    if not remaining:
        raise ConfigError(
            "this is the only audience you have, and the agent cannot run with "
            "none. Add another one before deleting this."
        )
    document["niches"] = remaining
    write_config(tenant_id, document)


# --------------------------------------------------------------------------- #
# Sending rules
# --------------------------------------------------------------------------- #

def read_rules(tenant_id: str) -> dict[str, Any]:
    document = read_config(tenant_id)
    config = load_tenant_config(tenant_id)

    # The follow-up plan comes from the merged cadence, so what is shown is
    # what would actually happen rather than only this file's overrides.
    plan: list[dict[str, Any]] = []
    try:
        cadence = config.cadence_for("both")
    except ConfigError:
        cadence = {"steps": []}
    for step in cadence.get("steps", []):
        name = str(step.get("name", ""))
        plan.append(
            {
                "step": int(step.get("step", len(plan) + 1)),
                "name": _step_label(name, int(step.get("step", len(plan) + 1))),
                "wait_days": int(step.get("wait_days", 0)),
                "channel": "linkedin" if "linkedin" in name else "email",
            }
        )

    compliance = document.get("compliance") or {}
    channels = document.get("channels") or {}
    runtime = document.get("runtime") or {}

    return {
        "regions": list(document.get("regions") or []),
        "language_map": dict(document.get("language_map") or {}),
        "channels": {
            "enabled": list(channels.get("enabled") or ["email"]),
            "preference_when_both": channels.get("preference_when_both", "both"),
        },
        "fit_score_threshold": int(document.get("fit_score_threshold", 60)),
        "max_leads_per_run": int(document.get("max_leads_per_run", 40)),
        "daily_send_limits": dict(document.get("daily_send_limits") or {}),
        "require_approval_before_send": bool(
            compliance.get("require_approval_before_send", True)
        ),
        "blocked_domains": list(compliance.get("blocked_domains") or []),
        "follow_ups": plan,
        "test_mode_default": bool(runtime.get("test_mode_default", True)),
    }


def _step_label(name: str, number: int) -> str:
    """Turn a cadence step's internal name into something readable."""
    known = {
        "opener": "First email",
        "email_opener": "First email",
        "value_nudge": "Second email",
        "email_value_nudge": "Second email",
        "breakup": "Last email",
        "email_breakup": "Last email",
        "connection_request": "LinkedIn request",
        "linkedin_connect": "LinkedIn request",
        "post_acceptance_dm": "LinkedIn message",
        "followup_dm": "LinkedIn follow-up",
    }
    if name in known:
        return known[name]
    if number == 1:
        return "First message"
    return f"Follow-up {number - 1}"


def save_rules(tenant_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    document = read_config(tenant_id)

    if "fit_score_threshold" in patch:
        value = int(patch["fit_score_threshold"])
        if not 0 <= value <= 100:
            raise ConfigError("the match bar has to be between 0 and 100")
        document["fit_score_threshold"] = value

    if "max_leads_per_run" in patch:
        value = int(patch["max_leads_per_run"])
        if value < 1:
            raise ConfigError("a search has to look for at least one business")
        document["max_leads_per_run"] = value

    if "require_approval_before_send" in patch:
        compliance = document.setdefault("compliance", {})
        compliance["require_approval_before_send"] = bool(
            patch["require_approval_before_send"]
        )

    if "blocked_domains" in patch:
        compliance = document.setdefault("compliance", {})
        compliance["blocked_domains"] = [
            str(domain).strip().lower()
            for domain in patch["blocked_domains"]
            if str(domain).strip()
        ]

    if "channels" in patch:
        channels = document.setdefault("channels", {})
        incoming = patch["channels"] or {}
        if incoming.get("enabled"):
            channels["enabled"] = list(incoming["enabled"])
        if incoming.get("preference_when_both"):
            channels["preference_when_both"] = incoming["preference_when_both"]

    if "language_map" in patch:
        document["language_map"] = dict(patch["language_map"])

    if "test_mode_default" in patch:
        runtime = document.setdefault("runtime", {})
        runtime["test_mode_default"] = bool(patch["test_mode_default"])

    write_config(tenant_id, document)
    return read_rules(tenant_id)


# --------------------------------------------------------------------------- #
# Rules for Leo
# --------------------------------------------------------------------------- #

def read_agent_rules(tenant_id: str) -> list[dict[str, Any]]:
    document = read_config(tenant_id)
    rules = document.get("agent_rules") or []
    return [
        {
            "id": str(rule.get("id", "")),
            "kind": rule.get("kind", "always"),
            "text": str(rule.get("text", "")),
            "created_at": str(rule.get("created_at", "")),
        }
        for rule in rules
        if rule.get("text")
    ]


def add_agent_rule(tenant_id: str, kind: str, text: str) -> dict[str, Any]:
    if kind not in ("always", "never"):
        raise ConfigError("an instruction is either an 'always' or a 'never'")
    text = text.strip()
    if not text:
        raise ConfigError("say what Leo should do")

    document = read_config(tenant_id)
    rules = document.setdefault("agent_rules", [])
    entry = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "text": text,
        "created_at": utcnow(),
    }
    rules.append(entry)
    write_config(tenant_id, document)
    return entry


def delete_agent_rule(tenant_id: str, rule_id: str) -> None:
    document = read_config(tenant_id)
    rules = document.get("agent_rules") or []
    remaining = [rule for rule in rules if str(rule.get("id")) != rule_id]
    if len(remaining) == len(rules):
        raise ConfigError("no such instruction")
    document["agent_rules"] = remaining
    write_config(tenant_id, document)


# --------------------------------------------------------------------------- #
# Provider selection
#
# An editor for the tenant YAML's `providers:` block, not a parallel store.
# The same `write_config` path validates every write with the agent's own
# validator, so a selection the pipeline would reject cannot be saved.
# --------------------------------------------------------------------------- #

def read_selection(tenant_id: str) -> dict[str, dict[str, Any]]:
    """
    What this workspace has chosen for each capability, and whether it can run.

    Answered through the capability layer's own resolver rather than by reading
    the YAML directly, so this screen and the pipeline can never disagree about
    which provider is in effect -- including the legacy fields and the .env
    fallbacks the resolver honours.
    """
    from src.nodes.n0_config_load import load_tenant_config
    from src.providers import describe

    return describe(load_tenant_config(tenant_id))


def save_selection(
    tenant_id: str,
    capability: str,
    *,
    primary: str,
    fallback: str = "",
    settings: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Point one capability at a provider.

    Validates against the registry before touching the file: a provider that
    cannot do the job it is being assigned to is refused here, with the list
    of ones that can, rather than written and then rejected by `_validate`
    with a message about a config file.

    `settings` is the non-secret configuration for a custom endpoint -- base
    URL, auth style, extra headers, model name. A `token` key is refused: a
    credential in this file would be a plaintext secret in something that gets
    copied, diffed and shared, and the encrypted store exists precisely so
    that cannot happen.
    """
    from src.providers import registry
    from src.providers.base import Capability

    try:
        wanted = Capability(capability)
    except ValueError:
        raise ConfigError(
            f"'{capability}' is not something this product needs done. "
            f"Expected one of {sorted(c.value for c in Capability)}."
        ) from None

    allowed = registry.selectable_ids(wanted)
    for label, chosen in (("primary", primary), ("fallback", fallback)):
        if not chosen:
            continue
        if chosen not in allowed:
            spec = registry.PROVIDERS_BY_ID.get(chosen)
            if spec is not None and not spec.enabled:
                raise ConfigError(
                    f"{spec.display_name} is not built yet, so it cannot be "
                    "selected."
                )
            raise ConfigError(
                f"'{chosen}' cannot be the {label} for that job. "
                f"Choose one of {allowed}."
            )
    if not primary:
        raise ConfigError("Choose a provider.")

    clean_settings = dict(settings or {})
    if clean_settings.pop("token", None) is not None:
        raise ConfigError(
            "A key cannot be saved here. Save it through the connection itself, "
            "where it is encrypted."
        )

    document = read_config(tenant_id)
    block = document.get("providers")
    if not isinstance(block, dict):
        document["providers"] = {}
        block = document["providers"]

    entry = block.get(capability)
    if not isinstance(entry, dict):
        block[capability] = {}
        entry = block[capability]

    entry["primary"] = primary
    if fallback:
        entry["fallback"] = fallback
    else:
        entry.pop("fallback", None)

    if clean_settings:
        entry["settings"] = clean_settings
    else:
        entry.pop("settings", None)

    write_config(tenant_id, document)
    return read_selection(tenant_id)


def save_capability_settings(
    tenant_id: str, capability: str, settings: dict[str, Any]
) -> dict[str, Any]:
    """
    Write one capability's non-secret settings without changing its provider.

    Used when somebody configures a custom endpoint: the URL and auth style
    belong in the config file, the key belongs in the encrypted store, and
    filling in the form should not silently switch which provider the
    capability uses. That is what the selection endpoint is for.

    A `token` key is refused here for the same reason it is refused by
    `save_selection`: this file gets copied, diffed and shared.
    """
    from src.providers.base import Capability

    try:
        Capability(capability)
    except ValueError:
        raise ConfigError(f"'{capability}' is not something this product needs done.") from None

    clean = {
        key: value
        for key, value in (settings or {}).items()
        if str(value).strip() != ""
    }
    if clean.pop("token", None) is not None:
        raise ConfigError(
            "A key cannot be saved here. It is stored encrypted instead."
        )

    document = read_config(tenant_id)
    block = document.get("providers")
    if not isinstance(block, dict):
        document["providers"] = {}
        block = document["providers"]
    entry = block.get(capability)
    if not isinstance(entry, dict):
        block[capability] = {}
        entry = block[capability]

    if clean:
        entry["settings"] = clean
    else:
        entry.pop("settings", None)

    # `_validate` requires a primary once a capability block exists, so a
    # settings-only write on a capability nobody has chosen yet has to name
    # one. The registry default is what the resolver would have used anyway.
    if not entry.get("primary"):
        from src.providers import registry

        entry["primary"] = registry.default_for(capability)

    write_config(tenant_id, document)
    return dict(clean)


def read_capability_settings(tenant_id: str, capability: str) -> dict[str, Any]:
    """One capability's non-secret settings. Never includes a key."""
    document = read_config(tenant_id)
    block = document.get("providers")
    if not isinstance(block, dict):
        return {}
    entry = block.get(capability)
    if not isinstance(entry, dict):
        return {}
    settings = entry.get("settings")
    if not isinstance(settings, dict):
        return {}
    return {str(k): v for k, v in settings.items() if k != "token"}
