"""
settings.py -- the business profile, the sending rules, and Leo's instructions.

All three live in the workspace's own configuration file. Every write is
validated with the agent's own validator before it replaces the file, so a save
from the UI can never leave a configuration the next run would reject.

On the profile: the shipped configuration has template values in the identity
fields rather than anybody's real name, and `backend/workspace.py` reports
those as empty. That is what makes a fresh install show a genuinely blank
profile with its prompts, instead of a fabricated sender that could end up at
the bottom of a real email.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend import workspace
from src.reliability import ConfigError

router = APIRouter(prefix="/api", tags=["settings"])


class ProfilePatch(BaseModel):
    """Only the fields present are written. Everything is optional and blank."""

    business_name: str | None = None
    website: str | None = None
    what_you_sell: str | None = None
    who_you_serve: str | None = None
    problems_you_solve: str | None = None
    what_makes_you_different: str | None = None
    services: str | None = None
    target_markets: str | None = None
    tone_of_voice: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    postal_address: str | None = None
    linkedin_account: str | None = None


class RulesPatch(BaseModel):
    fit_score_threshold: int | None = None
    max_leads_per_run: int | None = None
    require_approval_before_send: bool | None = None
    blocked_domains: list[str] | None = None
    channels: dict[str, Any] | None = None
    language_map: dict[str, str] | None = None
    test_mode_default: bool | None = None


class AgentRuleBody(BaseModel):
    kind: str = Field(pattern="^(always|never)$")
    text: str


# --------------------------------------------------------------------------- #
# Which workspace this is
# --------------------------------------------------------------------------- #

@router.get("/workspace")
def get_workspace() -> dict[str, str]:
    """
    The internal namespace, for the Technical details panel.

    The user is never asked to choose or create one -- one deployment serves one
    business -- but the agent namespaces every file it writes by this id, so the
    client has to carry it on each read.
    """
    return {"tenant_id": workspace.resolve_tenant_id()}


# --------------------------------------------------------------------------- #
# Business profile
# --------------------------------------------------------------------------- #

@router.get("/profile")
def get_profile() -> dict[str, str]:
    try:
        return workspace.read_profile(workspace.resolve_tenant_id())
    except ConfigError as exc:
        raise HTTPException(500, detail=str(exc))


@router.put("/profile")
def put_profile(patch: ProfilePatch) -> dict[str, str]:
    body = {key: value for key, value in patch.model_dump().items() if value is not None}
    if not body:
        return workspace.read_profile(workspace.resolve_tenant_id())
    try:
        return workspace.save_profile(workspace.resolve_tenant_id(), body)
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc))


# --------------------------------------------------------------------------- #
# Sending rules
# --------------------------------------------------------------------------- #

@router.get("/rules")
def get_rules() -> dict[str, Any]:
    try:
        return workspace.read_rules(workspace.resolve_tenant_id())
    except ConfigError as exc:
        raise HTTPException(500, detail=str(exc))


@router.put("/rules")
def put_rules(patch: RulesPatch) -> dict[str, Any]:
    body = {key: value for key, value in patch.model_dump().items() if value is not None}
    if not body:
        return workspace.read_rules(workspace.resolve_tenant_id())
    try:
        return workspace.save_rules(workspace.resolve_tenant_id(), body)
    except (ConfigError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc))


# --------------------------------------------------------------------------- #
# Rules for Leo
# --------------------------------------------------------------------------- #

@router.get("/agent-rules")
def get_agent_rules() -> list[dict[str, Any]]:
    try:
        return workspace.read_agent_rules(workspace.resolve_tenant_id())
    except ConfigError as exc:
        raise HTTPException(500, detail=str(exc))


@router.post("/agent-rules")
def post_agent_rule(body: AgentRuleBody) -> dict[str, Any]:
    try:
        return workspace.add_agent_rule(
            workspace.resolve_tenant_id(), body.kind, body.text
        )
    except ConfigError as exc:
        raise HTTPException(422, detail=str(exc))


@router.delete("/agent-rules/{rule_id}")
def delete_agent_rule(rule_id: str) -> dict[str, str]:
    try:
        workspace.delete_agent_rule(workspace.resolve_tenant_id(), rule_id)
    except ConfigError as exc:
        raise HTTPException(404, detail=str(exc))
    return {"id": rule_id}
