"""
counters.py -- durable, period-resetting counters for free-tier quotas.

DEVIATION NOTE: not in the Section 2 layout. Section 1 requires that every hard
numeric free-tier limit (Hunter 25/month, Gmail ~500/day, Brevo ~300/day, DeepL
~500k chars/month, Places monthly credit) be "enforced in code with a
counter/guard, not just a comment". Five integrations need the identical
mechanism -- a stored count plus a stored period key that triggers a reset --
so it lives here once instead of being reimplemented five times, subtly
differently.

Storage is a small JSON file (see settings.counter_path). Writes are atomic
(write temp, then replace) so a process killed mid-write cannot corrupt a quota
file and silently reset someone's monthly budget to zero.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from src.settings import counter_path
from src.state import utcnow

Period = Literal["day", "month", "never"]


class QuotaExceeded(RuntimeError):
    """
    Raised when a call would exceed a free-tier ceiling.

    Callers are expected to CATCH this and degrade (skip the lookup, queue the
    send for tomorrow) rather than let it kill a batch. It is a control-flow
    signal, not a crash.
    """

    def __init__(self, name: str, used: int, cap: int, resets: str):
        self.name, self.used, self.cap, self.resets = name, used, cap, resets
        super().__init__(
            f"quota '{name}' exhausted: {used}/{cap} used, resets {resets}"
        )


def _period_key(period: Period, now: datetime | None = None) -> str:
    now = now or utcnow()
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "month":
        return now.strftime("%Y-%m")
    return "all-time"


def _next_reset(period: Period, now: datetime | None = None) -> str:
    now = now or utcnow()
    if period == "day":
        return "at 00:00 UTC tomorrow"
    if period == "month":
        return f"on {now.year + (now.month == 12)}-{(now.month % 12) + 1:02d}-01 UTC"
    return "never"


@dataclass
class Counter:
    """
    A durable counter that resets when its period rolls over.

        c = Counter("hunter_lookups", cap=25, period="month")
        c.check(1)      # raises QuotaExceeded if it would not fit
        c.consume(1)    # records usage

    `check` and `consume` are separate so an integration can verify budget
    BEFORE spending a network round trip, and only record usage on success.
    """

    name: str
    cap: int
    period: Period = "month"
    tenant_id: str | None = None

    @property
    def path(self) -> Path:
        return counter_path(self.name, self.tenant_id)

    # -- storage ------------------------------------------------------------ #

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"period_key": _period_key(self.period), "count": 0}
        current = _period_key(self.period)
        if raw.get("period_key") != current:
            # Period rolled over -- the budget is fresh again.
            return {"period_key": current, "count": 0, "previous": raw}
        return raw

    def _write(self, data: dict) -> None:
        data["updated_at"] = utcnow().isoformat()
        data["cap"] = self.cap
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)   # atomic on Windows and POSIX
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- api ---------------------------------------------------------------- #

    @property
    def used(self) -> int:
        return int(self._read().get("count", 0))

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def would_exceed(self, amount: int = 1) -> bool:
        return self.used + amount > self.cap

    def check(self, amount: int = 1) -> None:
        """Raise QuotaExceeded if `amount` would not fit in the budget."""
        used = self.used
        if used + amount > self.cap:
            raise QuotaExceeded(self.name, used, self.cap, _next_reset(self.period))

    def consume(self, amount: int = 1) -> int:
        """Record usage after a successful call. Returns the new total."""
        data = self._read()
        data["count"] = int(data.get("count", 0)) + amount
        self._write(data)
        return data["count"]

    def check_and_consume(self, amount: int = 1) -> int:
        """Atomic-ish guard for callers that cannot separate the two steps."""
        self.check(amount)
        return self.consume(amount)

    def reset(self) -> None:
        """Manual reset. Used by tests and by the operator after a plan change."""
        self._write({"period_key": _period_key(self.period), "count": 0})

    def status(self) -> str:
        return f"{self.name}: {self.used}/{self.cap} used, resets {_next_reset(self.period)}"


# --------------------------------------------------------------------------- #
# The concrete free-tier counters this system enforces
# --------------------------------------------------------------------------- #

def hunter_counter() -> Counter:
    """Hunter.io free plan: 25 lookups/month, global (not per tenant)."""
    from src.settings import hunter_monthly_cap

    return Counter("hunter_lookups", cap=hunter_monthly_cap(), period="month")


def deepl_counter() -> Counter:
    """DeepL API Free: 500k characters/month, global."""
    from src.settings import deepl_monthly_char_cap

    return Counter("deepl_chars", cap=deepl_monthly_char_cap(), period="month")


def places_counter() -> Counter:
    """Google Places: guard the free monthly credit with a request cap."""
    from src.settings import places_monthly_cap

    return Counter("places_requests", cap=places_monthly_cap(), period="month")


def send_counter(tenant_id: str, provider: str) -> Counter:
    """
    Daily send cap, PER TENANT PER PROVIDER (N6a). Two tenants sharing one Gmail
    account is an operator mistake, but the counters are still separate so one
    tenant cannot silently eat another's visible budget.
    """
    from src.settings import daily_send_cap

    return Counter(
        f"sends__{provider}", cap=daily_send_cap(provider),
        period="day", tenant_id=tenant_id,
    )
