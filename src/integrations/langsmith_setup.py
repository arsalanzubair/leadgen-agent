"""
langsmith_setup.py -- tracing.

Wires LangSmith free-tier tracing so every node execution is tagged with
tenant_id and region, which is what makes per-tenant and per-region filtering
possible in the LangSmith UI (Section 1).

This is a no-op when LANGSMITH_TRACING is not enabled. Tracing must never be
required for a run to succeed -- an observability outage is not a reason to
stop sending.

Env: LANGSMITH_TRACING, LANGSMITH_API_KEY, LANGSMITH_PROJECT, LANGSMITH_ENDPOINT
"""

from __future__ import annotations

import os
from typing import Any

from src.reliability import log
from src.settings import env, env_bool
from src.state import LeadState


def tracing_enabled() -> bool:
    return env_bool("LANGSMITH_TRACING", False) and bool(env("LANGSMITH_API_KEY"))


def configure() -> bool:
    """
    Set the environment variables LangChain's tracer reads. Returns whether
    tracing is actually on, so a CLI can say so in its banner instead of the
    operator wondering why the LangSmith project is empty.
    """
    if not tracing_enabled():
        os.environ["LANGSMITH_TRACING"] = "false"
        os.environ.pop("LANGCHAIN_TRACING_V2", None)
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGCHAIN_TRACING_V2"] = "true"     # older LangChain reads this
    os.environ["LANGSMITH_API_KEY"] = env("LANGSMITH_API_KEY")
    os.environ["LANGSMITH_PROJECT"] = env("LANGSMITH_PROJECT", "leadgen-agent")
    os.environ["LANGSMITH_ENDPOINT"] = env(
        "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
    )
    log.info("LangSmith tracing enabled (project=%s)", os.environ["LANGSMITH_PROJECT"])
    return True


def run_config(state: LeadState, *, node_name: str = "", run_id: str = "") -> dict[str, Any]:
    """
    The LangChain run config for one lead.

    Tags are what you filter on in the LangSmith UI, so tenant and region are
    tags rather than only metadata. Section 1 asks specifically for both.
    """
    tenant_id = state.get("tenant_id", "unknown")
    region = state.get("region", "unknown")
    tags = [
        f"tenant:{tenant_id}",
        f"region:{region}",
        f"niche:{state.get('niche_id', 'unknown')}",
        f"channel:{state.get('channel', 'unknown')}",
    ]
    if node_name:
        tags.append(f"node:{node_name}")
    if state.get("dry_run"):
        tags.append("dry_run")

    return {
        "tags": tags,
        "metadata": {
            "tenant_id": tenant_id,
            "region": region,
            "niche_id": state.get("niche_id", ""),
            "lead_id": state.get("lead_id", ""),
            "company_name": state.get("company_name", ""),
            "sequence_step": state.get("sequence_step", 0),
            "run_id": run_id,
            "dry_run": bool(state.get("dry_run")),
        },
        "run_name": f"{node_name or 'lead'}:{state.get('company_name', '')}"[:64],
    }
