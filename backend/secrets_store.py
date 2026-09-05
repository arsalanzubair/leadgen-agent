"""
secrets_store.py -- the encrypted, per-workspace store for the user's API keys.

READ THIS BEFORE CHANGING ANYTHING HERE. This is the one module in the whole
project where a mistake means somebody's credentials leak.

The design, and why:

  * Keys are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256,
    authenticated), keyed by SECRETS_MASTER_KEY from the environment. The
    master key is NEVER written to the store, never returned by an endpoint,
    and never committed. It is generated once, on first run, into
    `.secrets/master.key` with the narrowest file permissions the platform
    allows -- and the operator is told, loudly, to back it up, because losing
    it means every saved key has to be re-entered.

  * The store is WRITE-ONLY from the outside. `get()` exists for one caller --
    `src/settings.py`, running inside the agent process that needs the key to
    make the actual API call. No HTTP route calls `get()`. The API surface can
    only ask "is there a key, and what are its last four characters".

  * `last4` is deliberately four characters and comes from the END of the
    value. That is enough for a person to recognise which key they pasted and
    useless for reconstructing it. Provider key prefixes (`sk-`, `AIza`) are
    the identifying part, so showing the START would be worse.

  * Writes are atomic: a temporary file in the same directory, then
    `os.replace`. A crash mid-write leaves the previous store intact rather
    than a truncated file that fails to decrypt.

  * Every value is encrypted individually rather than the file as a whole, so
    one corrupted entry cannot take the rest down with it.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from src.settings import ROOT, _assert_safe_tenant_id, env

#: Where the master key lives when it is not supplied through the environment.
MASTER_KEY_FILE = ROOT / ".secrets" / "master.key"

#: Bumped if the on-disk shape ever changes, so an old store can be migrated
#: rather than silently misread.
STORE_VERSION = 1


class SecretsError(RuntimeError):
    """Something is wrong with the store or the master key."""


# --------------------------------------------------------------------------- #
# The master key
# --------------------------------------------------------------------------- #

def _load_or_create_master_key() -> bytes:
    """
    The master key, from the environment if set, else from a generated file.

    Preferring the environment matters for a real deployment: the key belongs
    in the platform's secret manager, not on the disk next to the ciphertext.
    The file is the single-machine convenience, and it is created with 0600 so
    another account on a shared box cannot read it.
    """
    from_env = env("SECRETS_MASTER_KEY")
    if from_env:
        try:
            Fernet(from_env.encode())
        except Exception as exc:  # noqa: BLE001 - any malformed key is fatal
            raise SecretsError(
                "SECRETS_MASTER_KEY is set but is not a valid Fernet key. It must "
                "be a 32-byte url-safe base64 string; generate one with:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            ) from exc
        return from_env.encode()

    if MASTER_KEY_FILE.exists():
        return MASTER_KEY_FILE.read_bytes().strip()

    MASTER_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    MASTER_KEY_FILE.write_bytes(key)
    try:
        # Owner read/write only. Best effort: Windows ignores the mode bits,
        # which is why the docstring tells the operator to prefer the env var.
        MASTER_KEY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_master_key())


def master_key_source() -> str:
    """Where the key came from, for the status endpoint. Never the key itself."""
    return "environment" if env("SECRETS_MASTER_KEY") else "file"


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

def store_path(tenant_id: str) -> Path:
    """One store per workspace, named by the workspace's own id."""
    _assert_safe_tenant_id(tenant_id)
    return ROOT / "config" / "tenants" / tenant_id / "secrets.enc"


@dataclass(frozen=True)
class SecretStatus:
    """What the outside world is allowed to know about a stored secret."""

    provider: str
    present: bool
    last4: str
    saved_at: str


def _read_raw(tenant_id: str) -> dict[str, Any]:
    path = store_path(tenant_id)
    if not path.exists():
        return {"version": STORE_VERSION, "secrets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretsError(
            f"the secrets store at {path} could not be read: {exc}. It has not "
            "been modified; move it aside to start fresh, and be aware that "
            "every saved key will need re-entering."
        ) from exc
    if not isinstance(data, dict) or "secrets" not in data:
        raise SecretsError(f"the secrets store at {path} is not in the expected shape")
    return data


def _write_raw(tenant_id: str, data: dict[str, Any]) -> None:
    path = store_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so os.replace is atomic rather than a cross-device copy.
    temporary = path.with_suffix(".enc.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(temporary, path)


def put(tenant_id: str, provider: str, values: dict[str, str], saved_at: str) -> SecretStatus:
    """
    Encrypt and store one provider's values, replacing anything already there.

    `values` may hold several fields (a mailbox needs an address and a
    password); each is encrypted separately.
    """
    if not values:
        raise SecretsError("nothing to save")

    fernet = _fernet()
    data = _read_raw(tenant_id)
    encrypted = {
        name: fernet.encrypt(str(value).encode()).decode()
        for name, value in values.items()
        if str(value).strip()
    }
    if not encrypted:
        raise SecretsError("nothing to save: every value was blank")

    primary = _primary_value(values)
    data["secrets"][provider] = {
        "values": encrypted,
        "last4": primary[-4:] if len(primary) >= 4 else "",
        "saved_at": saved_at,
    }
    _write_raw(tenant_id, data)
    return SecretStatus(provider, True, data["secrets"][provider]["last4"], saved_at)


def _primary_value(values: dict[str, str]) -> str:
    """
    Which field the last-four fragment comes from.

    The longest value, which in practice is the key or token rather than an
    address or a spreadsheet id -- the thing a person would recognise.
    """
    return max((str(v) for v in values.values()), key=len, default="")


def delete(tenant_id: str, provider: str) -> bool:
    """Forget a provider's values entirely. True if there was something to forget."""
    data = _read_raw(tenant_id)
    if provider not in data["secrets"]:
        return False
    del data["secrets"][provider]
    _write_raw(tenant_id, data)
    return True


def status(tenant_id: str, provider: str) -> SecretStatus:
    """Whether a key is stored, and its last four characters. Never the key."""
    entry = _read_raw(tenant_id)["secrets"].get(provider)
    if not entry:
        return SecretStatus(provider, False, "", "")
    return SecretStatus(provider, True, entry.get("last4", ""), entry.get("saved_at", ""))


def stored_providers(tenant_id: str) -> list[str]:
    return sorted(_read_raw(tenant_id)["secrets"].keys())


def get(tenant_id: str, provider: str, field: str) -> str:
    """
    Decrypt one stored value.

    The ONLY intended caller is `src.settings.secret_for()`, inside the agent
    process that needs the value to make a real API call. Nothing that serves
    an HTTP response calls this, and nothing should: see the module docstring.

    Returns "" when absent, and raises on a value that cannot be decrypted --
    because a key that silently reads as empty looks exactly like a key that
    was never set, and the operator would spend an afternoon on it.
    """
    entry = _read_raw(tenant_id)["secrets"].get(provider)
    if not entry:
        return ""
    token = entry.get("values", {}).get(field)
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretsError(
            f"the stored {provider}.{field} for workspace '{tenant_id}' cannot be "
            "decrypted with the current SECRETS_MASTER_KEY. If the master key was "
            "changed or lost, the saved keys are unrecoverable and must be "
            "re-entered in Settings -> Connections."
        ) from exc


def get_all(tenant_id: str, provider: str) -> dict[str, str]:
    """Every stored field for one provider. Same warning as `get`."""
    entry = _read_raw(tenant_id)["secrets"].get(provider)
    if not entry:
        return {}
    return {field: get(tenant_id, provider, field) for field in entry.get("values", {})}
