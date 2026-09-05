"""
reliability.py -- Section 8 in one place: retry once, never crash a batch.

DEVIATION NOTE: not in the Section 2 layout. Section 8 requires that *every*
external API call retries once and that a repeated failure routes THAT LEAD to
needs_manual_review instead of raising, with every failure logged with
tenant_id, lead_id, node_name and the error. Eleven nodes and eleven
integrations need identical behaviour; implementing it eleven times guarantees
one of them eventually gets it wrong.

Two things are exported:

  * `retry_once(fn, ...)` -- for integration-level calls.
  * `@node` -- a decorator every node function wears. It catches anything the
    node raises, logs it, and returns a state update that flags the lead for
    manual review. The batch keeps moving.

`ConfigError` is the one deliberate exception to "never raise": N0 must HALT
the batch on a bad tenant config rather than process leads against defaults.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from typing import Any, Callable, TypeVar

from langgraph.errors import GraphBubbleUp

from src.state import LeadState, record_error, utcnow

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _build_logger() -> logging.Logger:
    logger = logging.getLogger("leadgen")
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger


log = _build_logger()


def log_failure(
    node_name: str, state: LeadState, error: BaseException | str, level: int = logging.ERROR
) -> None:
    """
    Section 8's logging contract: tenant_id, lead_id, node_name and the error,
    to stdout. LangSmith gets the same information through the run tags that
    integrations/langsmith_setup.py attaches.
    """
    log.log(
        level,
        "node=%s tenant=%s lead=%s company=%r error=%s",
        node_name,
        state.get("tenant_id", "?"),
        state.get("lead_id", "?"),
        state.get("company_name", "?"),
        error,
    )


class ConfigError(RuntimeError):
    """
    Tenant configuration is invalid. N0 raises this and it is deliberately NOT
    swallowed by @node: a bad config must halt before Discovery (Section 4, N0)
    rather than silently defaulting for every lead in the batch.
    """


class SkipLead(Exception):
    """
    A node deciding this lead should stop here for a non-error reason
    (unreachable, archived, suppressed). Carries the state update to apply.
    """

    def __init__(self, **updates: Any):
        self.updates = updates
        super().__init__(updates.get("archive_reason") or "skipped")


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #

def retry_once(
    fn: Callable[..., T],
    *args: Any,
    _label: str = "",
    _backoff_seconds: float = 1.0,
    _retry_on: tuple[type[BaseException], ...] = (Exception,),
    **kwargs: Any,
) -> T:
    """
    Call `fn`, and on failure call it exactly once more after a short backoff.

    Section 8 says "retry-once", so that is literally what this does -- no
    exponential ladder that turns a rate-limited free tier into a ban. The
    second failure propagates to the caller, which is @node for a node-level
    call or an explicit try/except for an integration that wants to degrade.
    """
    try:
        return fn(*args, **kwargs)
    except _retry_on as first:
        log.warning(
            "retrying %s after error: %s", _label or getattr(fn, "__name__", "call"), first
        )
        if _backoff_seconds > 0:
            time.sleep(_backoff_seconds)
        return fn(*args, **kwargs)


# --------------------------------------------------------------------------- #
# The node decorator
# --------------------------------------------------------------------------- #

def node(node_name: str) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    """
    Wrap a node function so no single lead's failure can crash a batch.

        @node("n3_qualification")
        def n3_qualification(state: LeadState) -> dict:
            ...

    Behaviour:
      * returns the node's update dict on success, with last_updated refreshed
      * `SkipLead` -> applies the carried updates (an ordinary control-flow exit)
      * `GraphBubbleUp` -> re-raised. This is LangGraph's OWN control flow, and
        `interrupt()` in N5 raises a GraphInterrupt through it. Swallowing that
        would silently convert every human-approval pause into a
        needs_manual_review flag and break the approval mechanism entirely --
        so it must be re-raised before the catch-all sees it.
      * `ConfigError` -> re-raised (N0 must halt the batch)
      * anything else -> logged, appended to `errors`, and the lead is flagged
        needs_manual_review so downstream conditional edges route it out
    """

    def decorate(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(state: LeadState, *args: Any, **kwargs: Any) -> dict:
            try:
                update = fn(state, *args, **kwargs) or {}
                update.setdefault("last_updated", utcnow())
                return update
            except SkipLead as skip:
                update = dict(skip.updates)
                update.setdefault("last_updated", utcnow())
                return update
            except GraphBubbleUp:
                raise                      # LangGraph's control flow, not an error
            except ConfigError:
                raise                      # halt the batch, by design
            except BaseException as exc:   # noqa: BLE001 -- deliberate catch-all
                log_failure(node_name, state, exc)
                return {
                    "needs_manual_review": True,
                    "manual_review_reason": f"{node_name} failed: {exc}",
                    "errors": record_error(state, node_name, exc),
                    "last_updated": utcnow(),
                }

        wrapper.__node_name__ = node_name  # type: ignore[attr-defined]
        return wrapper

    return decorate
