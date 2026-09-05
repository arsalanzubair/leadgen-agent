"""
main.py -- the settings service.

A deliberately small FastAPI app. It exists because three things in the product
have to really work rather than look right: storing the user's own API keys,
editing who they want to find, and remembering the instructions they give Leo.
Everything else the dashboard shows is lead data, which it reads from its own
sample layer until the agent's own HTTP surface exists.

It imports from `src/` rather than duplicating anything: the same config
loader, the same validator, the same LLM client the agent uses. A change to the
agent's rules is a change here too, automatically, which is the point.

Run it from the project root:

    python -m uvicorn backend.main:app --port 8000 --reload

The dashboard's dev server proxies /api to port 8000, so the client uses the
same same-origin path in development that it will use behind a reverse proxy in
production -- no CORS special case, no environment-specific base URL.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import secrets_store
from backend.routers import (
    integrations,
    niches,
    providers as provider_routes,
    settings as settings_routes,
)
from backend.workspace import resolve_tenant_id
from src.reliability import ConfigError
from src.settings import env, env_bool

log = logging.getLogger("outreachr.backend")

app = FastAPI(
    title="Outreachr settings service",
    version="1.0.0",
    description=(
        "Stores the user's own API keys, encrypted, and edits the workspace's "
        "targeting configuration. No endpoint here ever returns a saved key."
    ),
)

def _allowed_origins() -> list[str]:
    raw = env("BACKEND_CORS_ORIGINS", "http://localhost:5173")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# The dashboard's dev server proxies /api, so same-origin is the normal case and
# CORS is only needed when somebody runs the two on different hosts. It is
# therefore off unless explicitly asked for, and never a wildcard: this service
# holds the user's credentials, and a permissive default on a local port is
# exactly the kind of thing that goes unnoticed.
if env_bool("BACKEND_ALLOW_CORS", False):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
    )


app.include_router(integrations.router)
app.include_router(provider_routes.router)
app.include_router(niches.router)
app.include_router(settings_routes.router)


@app.exception_handler(ConfigError)
def handle_config_error(_request: Request, exc: ConfigError) -> JSONResponse:
    """
    A rejected configuration is the user's problem to fix, not a server fault.

    The agent's validator reports every problem it found at once rather than
    the first, so the message can be long -- and it is passed through intact,
    because a truncated list means a second failed save.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(secrets_store.SecretsError)
def handle_secrets_error(_request: Request, exc: secrets_store.SecretsError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict[str, Any]:
    """
    Is the service up, and is it able to do its job.

    Reports where the encryption key came from but never the key, and never any
    stored secret -- only which providers have one, which is the same thing the
    connections list already says.
    """
    tenant_id = resolve_tenant_id()
    try:
        stored = secrets_store.stored_providers(tenant_id)
        store_ok = True
        store_problem = ""
    except secrets_store.SecretsError as exc:
        stored = []
        store_ok = False
        store_problem = str(exc)

    return {
        "ok": store_ok,
        "workspace": tenant_id,
        "secrets_store": {
            "readable": store_ok,
            "master_key_from": secrets_store.master_key_source(),
            "connected_providers": stored,
            "problem": store_problem,
        },
    }
