"""
progress.py -- where a run says what it is doing while it is still doing it.

The graph has no notion of progress. `run_batch` hands back a finished batch
and everything in between is invisible from outside the process, which is fine
for a command-line run that prints as it goes and useless for a screen that
has to answer "what is happening right now".

Every node reports through here as it finishes. The default sink does nothing
at all: the command-line entry points and the test suite install none and
behave exactly as they did before this module existed. Reporting progress must
never become a precondition for running a batch.

One sink at a time, deliberately. Two batches reporting into one observer would
interleave their events with no way to tell them apart, and the settings
service refuses to start a second run while one is going for the same reason --
these are metered providers, and two concurrent runs spend twice as fast.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

# (node_name, payload) -- payload carries the lead and how the node ended.
Sink = Callable[[str, dict[str, Any]], None]

_lock = threading.Lock()
_sink: Sink | None = None


def install(sink: Sink | None) -> None:
    """Point progress at `sink`, or at nothing when None."""
    global _sink
    with _lock:
        _sink = sink


def active() -> bool:
    """Is anything listening. Used to refuse a second concurrent run."""
    with _lock:
        return _sink is not None


def emit(node_name: str, payload: dict[str, Any] | None = None) -> None:
    """
    Report that a node finished, and how.

    Never raises, and never holds the lock while the sink runs. A progress
    observer that can break a batch in flight is worse than no observer: the
    run is the thing that matters, and watching it is a convenience.
    """
    with _lock:
        sink = _sink
    if sink is None:
        return
    try:
        sink(node_name, payload or {})
    except Exception:  # noqa: BLE001 -- see the docstring
        pass
