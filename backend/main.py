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

    python -m backend.main

The port resolves as `BACKEND_PORT` -> `PORT` -> 8000, so it works both here
and on a platform that injects `PORT` (Render, Heroku, Fly, Cloud Run). The
host is `BACKEND_HOST`, default 127.0.0.1 -- a deployment needs 0.0.0.0, and
gets a warning rather than an automatic switch. `BACKEND_RELOAD=true` turns on
the auto-reloader. All of it is read through `src.settings`, so any of it can
come from the environment or from `.env` like everything else. Nothing is
hardcoded, so a stuck port is one variable away from being somebody else's
problem rather than a blocked morning.

`python -m uvicorn backend.main:app --port 8000` still works and is unchanged;
it just does not read those variables, because uvicorn's own CLI owns the
socket in that path.

The dashboard's dev server proxies /api to the same port -- it reads
`BACKEND_PORT` too -- so the client uses the same same-origin path in
development that it will use behind a reverse proxy in production: no CORS
special case, no environment-specific base URL.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import secrets_store
from backend.routers import (
    integrations,
    niches,
    providers as provider_routes,
    runs as run_routes,
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
app.include_router(run_routes.router)
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


# --------------------------------------------------------------------------- #
# Running it
# --------------------------------------------------------------------------- #

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

#: The port when nothing in the environment says otherwise.
DEFAULT_PORT = 8000


class BindConfig(NamedTuple):
    host: str
    port: int
    reload: bool
    #: Set when PORT points at a platform but the host is unreachable from it.
    warning: str


def resolve_bind() -> BindConfig:
    """
    Where to listen, from the environment.

    Separated from `_serve` so the precedence can be tested without opening a
    socket. The order is `BACKEND_PORT` -> `PORT` -> 8000:

      BACKEND_PORT  this project's own name for it, and the more specific of
                    the two -- somebody who set it meant it, so it wins.
      PORT          what Render, Heroku, Fly and Cloud Run inject. On those,
                    binding anything else points the router at a closed
                    socket, so it is honoured rather than ignored.

    Everything goes through `src.settings`, so either can come from a real
    environment variable or from `.env`, like the rest of the project.
    """
    from src import settings

    host = settings.env("BACKEND_HOST", "127.0.0.1")
    port = settings.env_int("BACKEND_PORT", settings.env_int("PORT", DEFAULT_PORT))
    reload_on_change = settings.env_bool("BACKEND_RELOAD", False)

    # A platform that injects PORT routes to the container's external
    # interface, and loopback is not reachable from outside it. This is a
    # warning rather than an automatic switch to 0.0.0.0: this service holds
    # the encrypted credential store, and inferring "expose on every
    # interface" from an environment variable is not a guess worth making
    # silently.
    warning = ""
    injected = settings.env("PORT")
    if injected and host in LOOPBACK_HOSTS:
        warning = (
            f"PORT is set ({injected}), which usually means a hosting platform, "
            f"but the service is bound to {host} -- which that platform cannot "
            f"reach.\nSet BACKEND_HOST=0.0.0.0 if this is a deployment."
        )

    return BindConfig(host=host, port=port, reload=reload_on_change, warning=warning)


def _serve() -> None:
    """
    Start the service on the configured host and port.

    The bind is probed before uvicorn is handed the socket, purely so a busy
    port produces a sentence somebody can act on instead of
    `WinError 10048: only one usage of each socket address is normally
    permitted`. The most common cause on Windows is an orphaned `--reload`
    worker: killing the PID that `netstat` reports kills the supervisor, while
    the child that actually inherited the socket keeps running, so the port
    stays busy and the PID stops existing.
    """
    import socket
    import sys

    import uvicorn

    host, port, reload_on_change, warning = resolve_bind()

    if warning:
        print(warning, file=sys.stderr)

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        print(
            f"Port {port} on {host} is already in use, so the settings service "
            f"cannot start.\n"
            f"  {exc}\n\n"
            f"Either free it:\n"
            f"    netstat -ano | findstr :{port}\n"
            f"  and stop the process that owns it -- on Windows also check for "
            f"an orphaned\n"
            f"  reload worker, whose parent PID may already be gone.\n\n"
            f"Or run on a different port without editing anything:\n"
            f"    BACKEND_PORT=8001 python -m backend.main\n"
            f"  (or set BACKEND_PORT in .env)",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    finally:
        probe.close()

    # `reload` needs an import string rather than the app object: the reloader
    # re-imports the module in a fresh process on every change.
    uvicorn.run(
        "backend.main:app" if reload_on_change else app,
        host=host,
        port=port,
        reload=reload_on_change,
    )


if __name__ == "__main__":
    _serve()
