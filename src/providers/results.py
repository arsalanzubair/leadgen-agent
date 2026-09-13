"""
results.py -- one shape for "did the provider call work", used everywhere.

THE RULE THIS EXISTS TO ENFORCE

`[]` must mean exactly one thing: the provider ran the operation and there was
nothing to return. It must never mean "the key was rejected", "the plan does
not include this endpoint", "the model was retired", or "the network died
halfway through". Before this module, several integrations conflated those --
`apollo.search()` and `places.search()` both caught every exception and handed
back an empty list, so a real outage and a legitimately quiet region were
indistinguishable by the time a lead ever saw them. That is how "0 Found"
ends up on screen for a failed search and a genuinely empty one alike.

THE SHAPE

`ProviderResult` carries a `status` (found results / ran and found nothing /
failed) alongside the data, so a caller can tell which happened without
inspecting the data itself. `ProviderError` is the failure half: a normalized
`code` from a small enum every integration maps its own errors onto, plus a
message safe to log, a separate one safe to show a user, whether retrying
could help, and what the user could do about it.

WHAT THIS MODULE DOES NOT DO

It does not replace `src.reliability`'s `@node` decorator or `retry_once` --
those are the per-lead, per-integration-call safety net that already existed
and is already tested. `call_with_retries` below is a different, narrower
thing: bounded retry-with-backoff for ONE outbound call that already knows how
to classify its own failures, used by the integration modules themselves
(`apollo.py`, `places.py`) before a result ever reaches a node. The two
compose rather than compete: a call wrapped in `call_with_retries` here can
still fail once, and `retry_once`/`@node` at the node layer is what turns that
single, well-explained failure into `needs_manual_review` instead of a crash.

Deliberately has no import of `src.reliability` or anything under `src.nodes`,
so nothing in the rest of the codebase can end up importing this in a circle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Error vocabulary
# --------------------------------------------------------------------------- #


class ErrorCode(str, Enum):
    """
    Every way a provider call can fail, collapsed to one small vocabulary.

    Deliberately provider-agnostic: Apollo's 403-on-search and a Gemini model
    retirement are different vendors and different HTTP codes, but both are
    "the credential is fine and the operation still cannot run" -- the
    dashboard and the retry logic need to treat that one way, regardless of
    which provider said it.
    """

    AUTH_ERROR = "auth_error"
    PERMISSION_DENIED = "permission_denied"
    PLAN_LIMITATION = "plan_limitation"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    MODEL_NOT_FOUND = "model_not_found"
    ENDPOINT_NOT_FOUND = "endpoint_not_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    BAD_RESPONSE = "bad_response"
    CONFIGURATION_ERROR = "configuration_error"
    UNKNOWN_PROVIDER_ERROR = "unknown_provider_error"


#: Retrying can plausibly help for these -- everything else is a fact about
#: the request or the account that a retry cannot change.
_RETRYABLE_CODES = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.PROVIDER_UNAVAILABLE,
        ErrorCode.NETWORK_ERROR,
        ErrorCode.TIMEOUT,
    }
)

#: One plain-language sentence per code, used when a call site does not
#: supply its own -- e.g. `_search_apollo`'s 403 branch writes a more specific
#: one naming the free-plan limitation. Never mentions internals.
_DEFAULT_USER_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_ERROR: "That connection's key was rejected.",
    ErrorCode.PERMISSION_DENIED: "That connection does not have permission for this.",
    ErrorCode.PLAN_LIMITATION: "This is not available on the connected account's plan.",
    ErrorCode.RATE_LIMITED: "The provider is rate-limiting requests right now.",
    ErrorCode.QUOTA_EXCEEDED: "This month's allowance for that provider is used up.",
    ErrorCode.INVALID_REQUEST: "The request was not something the provider accepted.",
    ErrorCode.UNSUPPORTED_OPERATION: "That connection cannot do this.",
    ErrorCode.MODEL_NOT_FOUND: "The configured model is not available.",
    ErrorCode.ENDPOINT_NOT_FOUND: "That provider address could not be found.",
    ErrorCode.PROVIDER_UNAVAILABLE: "The provider is having problems right now.",
    ErrorCode.NETWORK_ERROR: "Could not reach the provider.",
    ErrorCode.TIMEOUT: "The provider did not respond in time.",
    ErrorCode.BAD_RESPONSE: "The provider sent back something unexpected.",
    ErrorCode.CONFIGURATION_ERROR: "This is not set up yet.",
    ErrorCode.UNKNOWN_PROVIDER_ERROR: "Something unexpected happened talking to the provider.",
}

_DEFAULT_USER_ACTIONS: dict[ErrorCode, str] = {
    ErrorCode.AUTH_ERROR: "Reconnect it in Settings with a fresh key.",
    ErrorCode.PERMISSION_DENIED: "Check the account's plan or the key's scopes in Settings.",
    ErrorCode.PLAN_LIMITATION: "Use another connected option for this, or upgrade the plan.",
    ErrorCode.RATE_LIMITED: "Wait a little and try again.",
    ErrorCode.QUOTA_EXCEEDED: "Wait for the allowance to reset, or use another option.",
    ErrorCode.INVALID_REQUEST: "Try describing it differently.",
    ErrorCode.UNSUPPORTED_OPERATION: "Use a different connection for this.",
    ErrorCode.MODEL_NOT_FOUND: "Update the configured model in Settings.",
    ErrorCode.ENDPOINT_NOT_FOUND: "Check the connection's address in Settings.",
    ErrorCode.PROVIDER_UNAVAILABLE: "Try again shortly.",
    ErrorCode.NETWORK_ERROR: "Check this machine's internet connection and try again.",
    ErrorCode.TIMEOUT: "Try again shortly.",
    ErrorCode.BAD_RESPONSE: "Try again; if it keeps happening, the provider may be mid-outage.",
    ErrorCode.CONFIGURATION_ERROR: "Finish setting this up in Settings.",
    ErrorCode.UNKNOWN_PROVIDER_ERROR: "Try again; if it keeps happening, contact support.",
}


@dataclass
class ProviderError(Exception):
    """
    One normalized failure. A dataclass AND an exception: integrations that
    already raise can keep raising, and every caller gets the same fields
    whether it caught this directly or read it out of a `ProviderResult`.

    `message` is for logs -- it may include the provider's own wording, and
    is expected to already have gone through `redact.scrub` before it reaches
    here if it came from a raw HTTP body. `user_message` is what the
    dashboard shows and must never carry a credential, a stack trace, or a
    vendor's internal error string.
    """

    code: ErrorCode
    provider: str
    operation: str
    message: str
    user_message: str = ""
    retryable: bool = False
    retry_after: float | None = None
    http_status: int | None = None
    user_action: str = ""

    def __post_init__(self) -> None:
        if not self.user_message:
            self.user_message = _DEFAULT_USER_MESSAGES.get(
                self.code, _DEFAULT_USER_MESSAGES[ErrorCode.UNKNOWN_PROVIDER_ERROR]
            )
        if not self.user_action:
            self.user_action = _DEFAULT_USER_ACTIONS.get(
                self.code, _DEFAULT_USER_ACTIONS[ErrorCode.UNKNOWN_PROVIDER_ERROR]
            )

    def __str__(self) -> str:  # what str(exc) gives a caller that never learns this module
        return f"{self.provider}.{self.operation} [{self.code.value}]: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        """What the backend/dashboard boundary actually sends. No `message` --
        that is the log-facing field and may echo more of the provider's own
        text than should reach a browser."""
        return {
            "code": self.code.value,
            "provider": self.provider,
            "operation": self.operation,
            "user_message": self.user_message,
            "user_action": self.user_action,
            "retryable": self.retryable,
        }


# --------------------------------------------------------------------------- #
# The result envelope
# --------------------------------------------------------------------------- #


class ProviderStatus(str, Enum):
    SUCCESS_WITH_RESULTS = "success_with_results"
    SUCCESS_EMPTY = "success_empty"
    ERROR = "error"


@dataclass
class ProviderResult(Generic[T]):
    """
    What every provider call in this layer returns, instead of a bare value
    or a bare `[]`. `status` is the fact a caller must branch on;
    `error is not None` is exactly equivalent to `status is ERROR`.
    """

    status: ProviderStatus
    provider: str
    operation: str
    data: T
    error: ProviderError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is not ProviderStatus.ERROR

    @classmethod
    def success(
        cls,
        provider: str,
        operation: str,
        data: T,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> "ProviderResult[T]":
        """`status` is derived from whether `data` is empty, not chosen by the
        caller -- so a provider adapter cannot accidentally claim results it
        did not return."""
        try:
            has_results = len(data) > 0  # type: ignore[arg-type]
        except TypeError:
            has_results = data is not None
        status = ProviderStatus.SUCCESS_WITH_RESULTS if has_results else ProviderStatus.SUCCESS_EMPTY
        return cls(status=status, provider=provider, operation=operation, data=data, metadata=metadata or {})

    @classmethod
    def failure(
        cls,
        error: ProviderError,
        *,
        data: T | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ProviderResult[T]":
        return cls(
            status=ProviderStatus.ERROR,
            provider=error.provider,
            operation=error.operation,
            data=data,  # type: ignore[arg-type]
            error=error,
            metadata=metadata or {},
        )


# --------------------------------------------------------------------------- #
# Classifying a failure
# --------------------------------------------------------------------------- #


def classify_http_status(
    status_code: int,
    body_text: str,
    *,
    provider: str,
    operation: str,
    retry_after: float | None = None,
) -> ProviderError:
    """
    The generic HTTP -> ErrorCode mapping every integration can start from.

    A call site is free to raise something more specific first (Apollo's 403
    is a documented plan limitation, not a bare permission error) -- this is
    the fallback for everything that does not need its own wording.
    """
    snippet = (body_text or "").strip().replace("\n", " ")[:200]
    if status_code == 401:
        return ProviderError(
            ErrorCode.AUTH_ERROR, provider, operation,
            message=f"{status_code}: {snippet}", http_status=status_code,
        )
    if status_code == 403:
        return ProviderError(
            ErrorCode.PERMISSION_DENIED, provider, operation,
            message=f"{status_code}: {snippet}", http_status=status_code,
        )
    if status_code == 404:
        return ProviderError(
            ErrorCode.ENDPOINT_NOT_FOUND, provider, operation,
            message=f"{status_code}: {snippet}", http_status=status_code,
        )
    if status_code == 429:
        return ProviderError(
            ErrorCode.RATE_LIMITED, provider, operation,
            message=f"{status_code}: {snippet}", retryable=True,
            retry_after=retry_after, http_status=status_code,
        )
    if status_code in (400, 422):
        return ProviderError(
            ErrorCode.INVALID_REQUEST, provider, operation,
            message=f"{status_code}: {snippet}", http_status=status_code,
        )
    if status_code >= 500:
        return ProviderError(
            ErrorCode.PROVIDER_UNAVAILABLE, provider, operation,
            message=f"{status_code}: {snippet}", retryable=True, http_status=status_code,
        )
    return ProviderError(
        ErrorCode.UNKNOWN_PROVIDER_ERROR, provider, operation,
        message=f"{status_code}: {snippet}", http_status=status_code,
    )


def classify_exception(exc: BaseException, *, provider: str, operation: str) -> ProviderError:
    """
    Transport-level failures: no response ever came back to classify by
    status code. Matched by class name rather than `isinstance` against
    `requests`/`httpx` directly, so this module stays usable from an
    integration that imports neither.
    """
    name = type(exc).__name__
    module = type(exc).__module__
    text = str(exc)

    if name in ("Timeout", "TimeoutException", "ReadTimeout", "ConnectTimeout") or "timed out" in text.lower():
        return ProviderError(ErrorCode.TIMEOUT, provider, operation, message=text, retryable=True)
    if (
        "requests.exceptions" in module
        or "httpx" in module
        or name in ("ConnectionError", "RequestError", "OSError", "socket.error", "URLError")
        or isinstance(exc, (ConnectionError, OSError))
    ):
        return ProviderError(ErrorCode.NETWORK_ERROR, provider, operation, message=text, retryable=True)
    if isinstance(exc, ValueError) or name in ("JSONDecodeError",):
        return ProviderError(ErrorCode.BAD_RESPONSE, provider, operation, message=text)
    return ProviderError(ErrorCode.UNKNOWN_PROVIDER_ERROR, provider, operation, message=text)


# --------------------------------------------------------------------------- #
# Bounded retry, for one outbound call
# --------------------------------------------------------------------------- #


def call_with_retries(
    fn: Any,
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 8.0,
    sleep: Any = time.sleep,
    **kwargs: Any,
) -> Any:
    """
    Call `fn(*args, **kwargs)`, which must raise `ProviderError` on failure
    (via `classify_http_status`/`classify_exception`, or its own).

    Retries ONLY when `error.retryable` is true -- never for a rejected key,
    a plan limitation, an unsupported operation, or a missing model, because
    no amount of retrying changes any of those. Honors `error.retry_after`
    when the provider gave one (a 429's `Retry-After`), otherwise backs off
    exponentially from `base_delay`, capped at `max_delay`.

    Raises the final `ProviderError` once attempts are spent. `sleep` is
    injectable so tests never actually wait.
    """
    attempt = 0
    last: ProviderError | None = None
    while attempt < max_attempts:
        attempt += 1
        try:
            return fn(*args, **kwargs)
        except ProviderError as err:
            last = err
            if not err.retryable or attempt >= max_attempts:
                raise
            delay = err.retry_after if err.retry_after is not None else min(
                max_delay, base_delay * (2 ** (attempt - 1))
            )
            sleep(delay)
    assert last is not None  # max_attempts >= 1 is the only supported use
    raise last
