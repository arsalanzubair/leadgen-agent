"""
settings.py -- environment loading and per-tenant path resolution.

DEVIATION NOTE: this module is not in the Section 2 layout. It exists because
three separate requirements converge on it and duplicating them across eleven
integrations would be worse:

  * Section 7 (multi-tenancy): checkpoint DB, suppression list, counters and
    CRM target must ALL be resolvable strictly by tenant_id. That resolution
    logic belongs in exactly one place so it cannot drift.
  * Section 1: free-tier ceilings must be enforced in code -- the numbers have
    to be read from somewhere consistent.
  * Section 9: a global dry-run kill switch that overrides any CLI flag.

Nothing here reads a config/tenants/*.yaml file; that is N0's job. This module
only handles process-level environment and filesystem layout.

CREDENTIAL RESOLUTION ORDER (added when the dashboard grew a real settings
service): every credential is looked for in the workspace's ENCRYPTED STORE
first, and in `.env` second. The store is what the Connections screen writes
to, so a key entered in the UI takes effect without anybody editing a file --
and `.env` still works untouched for a headless install that never opens the
dashboard.

The store is reached through a guarded, lazy import of `backend/`, so this
module keeps working with `backend/` absent, uninstalled, or mid-refactor. If
it cannot be imported, resolution silently falls back to `.env` alone, which is
exactly the behaviour this file had before.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

#: Repository root (this file lives at <root>/src/settings.py).
ROOT = Path(__file__).resolve().parent.parent

# Load .env once, at import. `override=False` so a real environment variable
# (e.g. one injected by GitHub Actions) always beats the local .env file.
load_dotenv(ROOT / ".env", override=False)


# --------------------------------------------------------------------------- #
# Primitive env readers
# --------------------------------------------------------------------------- #

def env(key: str, default: str = "") -> str:
    """Read a string env var, trimmed. Missing or blank -> `default`."""
    value = os.environ.get(key, "")
    value = value.strip() if value else ""
    return value or default


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    raw = env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_list(key: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = env(key)
    if not raw:
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def env_for_tenant(prefix: str, tenant_id: str, default: str = "") -> str:
    """
    Read a per-tenant env var, e.g.
    `GOOGLE_SHEETS_SPREADSHEET_ID__example_tenant`, falling back to the
    unsuffixed `GOOGLE_SHEETS_SPREADSHEET_ID` if the tenant-specific one is
    unset. Section 7: per-tenant resolution, with a single-tenant convenience.
    """
    return env(f"{prefix}__{tenant_id}") or env(prefix, default)


# --------------------------------------------------------------------------- #
# Safety rails
# --------------------------------------------------------------------------- #

def global_dry_run() -> bool:
    """
    Process-wide kill switch. When true, N6a and N6b print instead of sending
    NO MATTER WHAT the CLI flag or tenant config says. Read live (not cached)
    so a test can flip it.
    """
    return env_bool("GLOBAL_DRY_RUN", True)


def blocked_domains() -> set[str]:
    """Domains that are never contacted, regardless of approval or clearance."""
    return set(env_list("BLOCKED_DOMAINS", ("example.com", "test.com")))


# --------------------------------------------------------------------------- #
# Per-tenant filesystem layout (Section 7)
# --------------------------------------------------------------------------- #

def _resolve_dir(key: str, default_rel: str) -> Path:
    raw = env(key, default_rel)
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def data_dir(key: str, default_rel: str) -> Path:
    path = _resolve_dir(key, default_rel)
    path.mkdir(parents=True, exist_ok=True)
    return path


def checkpoint_path(tenant_id: str) -> Path:
    """
    SQLite checkpoint DB for one tenant. NEVER shared: the tenant_id is in the
    filename, and `graph.build_graph` takes no path override that could
    accidentally point two tenants at one file.
    """
    _assert_safe_tenant_id(tenant_id)
    return data_dir("CHECKPOINT_DIR", ".data/checkpoints") / f"{tenant_id}.sqlite"


def counter_path(name: str, tenant_id: str | None = None) -> Path:
    """Durable counter file. Tenant-scoped when a tenant_id is supplied."""
    directory = data_dir("COUNTER_DIR", ".data/counters")
    if tenant_id:
        _assert_safe_tenant_id(tenant_id)
        return directory / f"{tenant_id}__{name}.json"
    return directory / f"{name}.json"


def suppression_path(tenant_id: str) -> Path:
    """Local suppression list for one tenant."""
    _assert_safe_tenant_id(tenant_id)
    return data_dir("SUPPRESSION_DIR", ".data/suppression") / f"{tenant_id}.json"


def seen_leads_path(tenant_id: str) -> Path:
    """N1 dedupe ledger: every dedupe_key this tenant has already discovered."""
    _assert_safe_tenant_id(tenant_id)
    return data_dir("COUNTER_DIR", ".data/counters") / f"{tenant_id}__seen_leads.json"


def linkedin_queue_path(tenant_id: str) -> Path:
    """N6b manual-send queue for one tenant."""
    _assert_safe_tenant_id(tenant_id)
    return data_dir("COUNTER_DIR", ".data/queues") / f"{tenant_id}__linkedin.json"


def dry_run_output_path(tenant_id: str) -> Path:
    """Where N9 writes CRM rows during a dry run instead of hitting Sheets."""
    _assert_safe_tenant_id(tenant_id)
    return data_dir("COUNTER_DIR", ".data/dry_run") / f"{tenant_id}__crm.csv"


def tenant_config_path(tenant_id: str) -> Path:
    _assert_safe_tenant_id(tenant_id)
    return ROOT / "config" / "tenants" / f"{tenant_id}.yaml"


COMPLIANCE_PROFILES_PATH = ROOT / "config" / "compliance_profiles.yaml"
CADENCES_PATH = ROOT / "config" / "cadences.yaml"
SAMPLE_LEADS_PATH = ROOT / "tests" / "fixtures" / "sample_leads.json"


def _assert_safe_tenant_id(tenant_id: str) -> None:
    """
    A tenant_id becomes a filename. Reject anything that could traverse out of
    the tenant's own directory -- this is the mechanical half of Section 7.
    """
    if not tenant_id or not isinstance(tenant_id, str):
        raise ValueError(f"tenant_id must be a non-empty string, got {tenant_id!r}")
    bad = set(tenant_id) - set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )
    if bad:
        raise ValueError(
            f"tenant_id {tenant_id!r} contains illegal characters {sorted(bad)}; "
            "only letters, digits, underscore and hyphen are allowed"
        )


#: Public alias. `backend/` needs this guard too, and reaching into a private
#: name across a package boundary is how a rename becomes a security bug.
def assert_safe_tenant_id(tenant_id: str) -> None:
    _assert_safe_tenant_id(tenant_id)


@lru_cache(maxsize=1)
def allowed_tenants() -> frozenset[str]:
    """Tenants this install may run. Empty set means 'no restriction'."""
    return frozenset(env_list("LEADGEN_TENANTS"))


# --------------------------------------------------------------------------- #
# Free-tier ceilings, read from env with the documented default
# --------------------------------------------------------------------------- #

def hunter_monthly_cap() -> int:
    return env_int("HUNTER_MONTHLY_LOOKUP_CAP", 25)


def deepl_monthly_char_cap() -> int:
    return env_int("DEEPL_MONTHLY_CHAR_CAP", 500_000)


def places_monthly_cap() -> int:
    return env_int("GOOGLE_PLACES_MONTHLY_REQUEST_CAP", 2000)


def daily_send_cap(provider: str) -> int:
    return {
        "gmail_smtp": env_int("GMAIL_DAILY_SEND_CAP", 500),
        "brevo": env_int("BREVO_DAILY_SEND_CAP", 300),
        # A mail server of your own will happily accept far more than this.
        # That is exactly why the ceiling is here: a new sending domain that
        # jumps to hundreds a day gets filtered rather than delivered.
        "smtp": env_int("SMTP_DAILY_SEND_CAP", 200),
    }.get(provider, 100)


# --------------------------------------------------------------------------- #
# Credentials: the encrypted store first, .env second
#
# Added when the dashboard grew a real Connections screen. Everything above
# this line behaves exactly as it did; this section only adds a source that is
# consulted BEFORE the environment.
#
# The lazy, guarded import matters. `backend/secrets_store` imports this
# module, so importing it at the top would be a cycle -- and a headless install
# that never runs the dashboard should not need `backend/` to exist at all.
# --------------------------------------------------------------------------- #

def workspace_tenant_id() -> str:
    """
    The single workspace this deployment serves.

    One deployment, one business: there is no switcher anywhere in the product.
    The id is still how every file, counter and suppression list is namespaced,
    so it has to be resolvable without asking anybody.

    Resolution order, most explicit first:
      1. LEADGEN_WORKSPACE
      2. the first entry in LEADGEN_TENANTS
      3. the only file in config/tenants/, when there is exactly one
      4. "example_tenant"
    """
    explicit = env("LEADGEN_WORKSPACE")
    if explicit:
        return explicit

    allowed = sorted(allowed_tenants())
    if allowed:
        return allowed[0]

    directory = ROOT / "config" / "tenants"
    if directory.is_dir():
        names = sorted(path.stem for path in directory.glob("*.yaml"))
        if len(names) == 1:
            return names[0]

    return "example_tenant"


def _store_lookup(env_var: str, tenant_id: str) -> str:
    """
    Ask the encrypted store for whatever satisfies this environment variable.

    Returns "" for every failure mode -- no store, no `backend/` package, no
    entry, an unreadable file -- because a credential lookup must never be the
    thing that takes a batch down. A genuinely broken store surfaces on the
    Connections screen and in /api/health, where somebody is looking.
    """
    try:
        from src.providers.registry import ENV_VAR_SOURCES
    except Exception:      # noqa: BLE001 - should not happen; see below
        return ""

    source = ENV_VAR_SOURCES.get(env_var)
    if not source:
        return ""

    # Only the store itself needs `backend/`, and only once we know there is
    # something to look up. A headless install with no settings service still
    # resolves every credential from .env.
    try:
        from backend import secrets_store
    except Exception:      # noqa: BLE001 - absent, broken or mid-install
        return ""

    if not source:
        return ""

    provider_id, field = source
    try:
        return secrets_store.get(tenant_id, provider_id, field)
    except Exception:      # noqa: BLE001 - see the docstring
        return ""


def _materialised_service_account(tenant_id: str) -> str:
    """
    The Google service-account key as a FILE PATH.

    gspread wants a path, and the store holds the JSON itself, so the blob is
    written out on demand to a per-workspace file under .secrets/ and the path
    returned. Written only when the contents differ, so a run does not rewrite
    it for every lead.
    """
    try:
        from backend import secrets_store
    except Exception:      # noqa: BLE001
        return ""

    try:
        blob = secrets_store.get(tenant_id, "google_sheets", "service_account_json")
    except Exception:      # noqa: BLE001
        return ""
    if not blob:
        return ""

    _assert_safe_tenant_id(tenant_id)
    target = ROOT / ".secrets" / tenant_id / "google_service_account.json"
    try:
        if not target.exists() or target.read_text(encoding="utf-8") != blob:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(blob, encoding="utf-8")
    except OSError:
        return ""
    return str(target)


def credential(env_var: str, tenant_id: str | None = None, default: str = "") -> str:
    """
    A credential, from the encrypted store first and `.env` second.

    Use this instead of `env()` for anything secret. `env()` itself routes
    through here for the variables the store knows about, so the eleven
    integration modules did not have to change -- but calling this directly is
    clearer at a new call site.
    """
    workspace = tenant_id or workspace_tenant_id()

    if env_var == "GOOGLE_SERVICE_ACCOUNT_FILE":
        path = _materialised_service_account(workspace)
        if path:
            return path

    stored = _store_lookup(env_var, workspace)
    if stored:
        return stored

    # `_env_from_environment`, not `env`: the wrapper defined below routes
    # credential variables through this function, so calling `env` here would
    # recurse until the stack ran out.
    return _env_from_environment(env_var, default)


#: Variables the store may hold. Kept here as a plain frozenset so `env()` can
#: decide whether a lookup is even worth attempting without importing anything.
#: It is a superset of what the provider registry declares -- a name appearing
#: here that the registry does not know about simply never matches.
#: Variables that MIGHT be answered by the encrypted store, so `env()` knows
#: which lookups are worth routing through `credential()`.
#:
#: Derived from the provider registry rather than listed here, because a
#: hand-kept copy drifts silently in the worst direction: a key the user saved
#: through Connections would be stored, shown as connected, and then never
#: read, because nothing routed its variable through the store.
#:
#: Cached on first use. `env()` is called dozens of times per lead and must
#: not rebuild this or re-import anything per call.
_STORED_ENV_VARS_CACHE: frozenset[str] | None = None


def _stored_env_vars() -> frozenset[str]:
    global _STORED_ENV_VARS_CACHE
    if _STORED_ENV_VARS_CACHE is None:
        try:
            from src.providers.registry import ENV_VAR_SOURCES

            names = set(ENV_VAR_SOURCES)
        except Exception:      # noqa: BLE001
            names = set()
        # Not a registry field: the store holds the service-account JSON and
        # gspread wants a path, so this one is answered by writing the blob out
        # (see `_materialised_service_account`) rather than by a lookup.
        names.add("GOOGLE_SERVICE_ACCOUNT_FILE")
        _STORED_ENV_VARS_CACHE = frozenset(names)
    return _STORED_ENV_VARS_CACHE

# Wrap the original `env` so every existing caller picks up stored credentials
# without an edit. Non-credential variables take the original path exactly, so
# the hot path for the dozens of ordinary settings is unchanged.
_env_from_environment = env


def env(key: str, default: str = "") -> str:            # type: ignore[no-redef]
    """
    Read a setting. Trimmed; missing or blank gives `default`.

    For the credential variables the registry knows about, the workspace's
    encrypted store is consulted first and the process environment second, so a
    key entered on the Connections screen takes effect with no file editing.
    Everything else reads the environment exactly as before.
    """
    direct = _env_from_environment(key, "")
    if direct:
        return direct
    if key in _stored_env_vars():
        stored = credential(key, None, "")
        if stored:
            return stored
    return default
