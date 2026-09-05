"""
integrations.py -- connect, test and disconnect a provider.

The contract, stated once and enforced by the shape of the responses:

  GET    /api/integrations                 status only, never a key
  POST   /api/integrations/{id}/test       one real call, saves nothing
  POST   /api/integrations/{id}            saves, encrypted, after a test
  DELETE /api/integrations/{id}            forgets it

No endpoint in this file returns a stored secret, in any form. The response
model literally has no field that could carry one -- `last4` is four
characters from the end of the value, which is enough to recognise a key and
useless for reconstructing it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import secrets_store
from backend.providers import (
    PROVIDERS,
    TestResult,
    get_provider,
    validate_values,
)
from backend.workspace import (
    read_capability_settings,
    resolve_tenant_id,
    save_capability_settings,
    utcnow,
)
from src.reliability import ConfigError

router = APIRouter(prefix="/api/integrations", tags=["connections"])


class ConnectionValues(BaseModel):
    """What the user typed. Travels one way only: in."""

    values: dict[str, str] = Field(default_factory=dict)


def _connection_dict(provider_id: str, tenant_id: str) -> dict[str, Any]:
    provider = get_provider(provider_id)
    status = secrets_store.status(tenant_id, provider_id)
    connected = status.present

    if provider.spec is not None and provider.spec.is_custom:
        # A custom endpoint set to `auth_style: none` legitimately has no key,
        # so a stored token is not what makes it configured -- a URL is.
        # Judging it by the store alone would show a working endpoint as
        # disconnected and invite the user to "fix" it.
        try:
            settings = read_capability_settings(
                tenant_id, provider.spec.capability.value
            )
        except ConfigError:
            settings = {}
        connected = connected or bool(str(settings.get("base_url", "")).strip())

    payload = provider.to_dict()
    payload.update(
        {
            "state": "connected" if connected else "not_connected",
            "last4": status.last4,
            "connected_at": status.saved_at or None,
            "problem": "",
            "alternative_connected": False,
        }
    )
    return payload


@router.get("")
def list_connections() -> list[dict[str, Any]]:
    """
    Every provider and whether this workspace has it connected.

    `alternative_connected` is filled in here rather than in the client so the
    "one of these is enough" wording on screen cannot disagree with what is
    actually stored.
    """
    tenant_id = resolve_tenant_id()
    connections = [_connection_dict(provider.id, tenant_id) for provider in PROVIDERS]

    connected_categories = {
        connection["category"]
        for connection in connections
        if connection["state"] == "connected"
    }
    for connection in connections:
        connection["alternative_connected"] = (
            connection["state"] != "connected"
            and connection["category"] in connected_categories
        )
    return connections


@router.post("/{provider_id}/test")
def test_connection(provider_id: str, body: ConnectionValues) -> dict[str, Any]:
    """
    Make one real, cheap call to the provider with these values.

    Nothing is saved by this. Testing before saving is the whole point: a key
    with a stray space in it should fail here, in front of the person who
    pasted it, and not three hours into a batch.
    """
    try:
        provider = get_provider(provider_id)
    except KeyError:
        raise HTTPException(404, detail=f"There is no provider called '{provider_id}'.")

    problem = validate_values(provider, body.values)
    if problem:
        return TestResult(False, problem).to_dict()

    try:
        result = provider.check(body.values)
    except Exception as exc:  # noqa: BLE001 - a provider client can raise anything
        result = TestResult(
            False,
            "The check could not be completed.",
            f"{exc.__class__.__name__}: {exc}",
        )
    return result.to_dict()


@router.post("/{provider_id}")
def save_connection(provider_id: str, body: ConnectionValues) -> dict[str, Any]:
    """
    Store the values, encrypted, after re-running the check.

    The check runs again here rather than trusting the client's word that one
    passed. The client disables its Save button until a test succeeds, but a
    button is a courtesy and this is the actual gate.
    """
    try:
        provider = get_provider(provider_id)
    except KeyError:
        raise HTTPException(404, detail=f"There is no provider called '{provider_id}'.")

    problem = validate_values(provider, body.values)
    if problem:
        raise HTTPException(422, detail=problem)

    try:
        result = provider.check(body.values)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            502,
            detail=f"The provider could not be reached, so nothing was saved. "
            f"({exc.__class__.__name__})",
        )

    if not result.ok:
        raise HTTPException(
            422,
            detail=f"{result.message} Nothing was saved."
            + (f" {result.detail}" if result.detail else ""),
        )

    tenant_id = resolve_tenant_id()

    # A custom endpoint's form covers two stores. The key is encrypted like any
    # other; the URL, auth style, headers and model name go in the tenant's
    # config, because that is where the resolver reads them and because they
    # are not secrets. The client posts one form and does not know any of this.
    if provider.spec is not None and provider.spec.is_custom:
        secret_fields = {
            f.name for f in provider.spec.connection_fields if f.kind == "secret"
        }
        settings = {
            name: value
            for name, value in body.values.items()
            if name not in secret_fields
        }
        try:
            save_capability_settings(
                tenant_id, provider.spec.capability.value, settings
            )
        except ConfigError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        values = {
            name: value for name, value in body.values.items() if name in secret_fields
        }
    else:
        values = dict(body.values)

    try:
        secrets_store.put(tenant_id, provider_id, values, utcnow())
    except secrets_store.SecretsError as exc:
        raise HTTPException(500, detail=str(exc))

    return _connection_dict(provider_id, tenant_id)


@router.delete("/{provider_id}")
def delete_connection(provider_id: str) -> dict[str, Any]:
    try:
        get_provider(provider_id)
    except KeyError:
        raise HTTPException(404, detail=f"There is no provider called '{provider_id}'.")

    tenant_id = resolve_tenant_id()
    try:
        secrets_store.delete(tenant_id, provider_id)
    except secrets_store.SecretsError as exc:
        raise HTTPException(500, detail=str(exc))
    return _connection_dict(provider_id, tenant_id)
