"""
What a run reports while it is running.

The bug these exist to prevent was silent: the stage map was keyed by the names
graph.py uses for its edges rather than the names the nodes pass to `@node`, so
every search worked perfectly and every stage reported "skipped". Nothing threw,
nothing logged, and the screen said the run had skipped all thirteen steps.
"""

from __future__ import annotations

import pytest

from backend import runs
from src import progress


@pytest.fixture(autouse=True)
def clean_registry():
    runs.__reset_for_tests()
    progress.install(None)
    yield
    runs.__reset_for_tests()
    progress.install(None)


# --------------------------------------------------------------------------- #
# The map between the graph and the screen
# --------------------------------------------------------------------------- #

def test_every_node_reports_into_a_stage():
    """
    A node whose name is not in the map reports nowhere.

    This is the guard for the original bug. If somebody renames a node in
    src/nodes/, this fails here rather than in front of a user.
    """
    unmapped, unfed = runs.check_stage_coverage()
    assert not unmapped, f"these nodes report into no stage: {sorted(unmapped)}"
    assert not unfed, f"these stages no node ever feeds: {sorted(unfed)}"


def test_the_map_is_keyed_by_the_decorator_name_not_the_edge_name():
    """
    The two vocabularies differ and only one of them arrives at the sink.

    `graph.py` calls the discovery node "discovery"; the node itself declares
    "n1_discovery", and that is what `progress.emit` is given.
    """
    assert "n1_discovery" in runs.STAGE_FOR_NODE
    assert "discovery" not in runs.STAGE_FOR_NODE


def test_the_stage_order_matches_the_dashboard():
    assert runs.RUN_STAGES[0] == "understanding"
    assert runs.RUN_STAGES[-1] == "saving"
    assert len(runs.RUN_STAGES) == 13
    # Every stage has a sentence, or the screen shows a bare status.
    assert set(runs.NOTES) == set(runs.RUN_STAGES)


# --------------------------------------------------------------------------- #
# Folding node events into a run record
# --------------------------------------------------------------------------- #

def _record() -> str:
    """A run record parked in the registry, with nothing reported yet."""
    run_id = "test_run_1"
    runs._runs[run_id] = {
        "run_id": run_id,
        "tenant_id": "t",
        "status": "running",
        "started_at": "2026-01-01T00:00:00+00:00",
        "stages": runs._blank_stages(),
    }
    return run_id


def stage_of(run_id: str, name: str) -> dict:
    record = runs._runs[run_id]
    return next(s for s in record["stages"] if s["stage"] == name)


def test_a_finished_node_completes_its_stage_and_starts_the_next():
    run_id = _record()
    runs._touch_stage(run_id, "n1_discovery", "done")

    finding = stage_of(run_id, "finding")
    assert finding["status"] == "completed"
    assert (finding["leads_in"], finding["leads_out"]) == (1, 1)
    assert finding["note"]

    # The screen should show something in progress, not a gap.
    assert stage_of(run_id, "researching")["status"] == "running"


def test_stages_the_run_routed_around_are_skipped_not_left_pending():
    """
    A lead that goes straight to the CRM never touches the middle stages.

    Leaving those as "waiting" would show a finished run as still having work
    to do.
    """
    run_id = _record()
    runs._touch_stage(run_id, "n9_crm_analytics", "done")
    assert stage_of(run_id, "saving")["status"] == "completed"
    assert stage_of(run_id, "drafting")["status"] == "skipped"


def test_a_diverted_lead_is_counted_apart_from_a_passing_one():
    run_id = _record()
    runs._touch_stage(run_id, "n3_qualification", "diverted")
    matching = stage_of(run_id, "matching")
    assert matching["leads_diverted"] == 1
    assert matching["leads_out"] == 0


def test_a_lead_waiting_for_a_person_leaves_the_stage_running():
    """N5's interrupt is a pause, not a completion."""
    run_id = _record()
    runs._touch_stage(run_id, "n5_human_approval", "paused")
    assert stage_of(run_id, "reviewing")["status"] == "running"


def test_a_failed_node_marks_the_stage_failed():
    run_id = _record()
    runs._touch_stage(run_id, "n4_personalization", "failed")
    assert stage_of(run_id, "drafting")["status"] == "failed"


def test_many_leads_accumulate_on_the_same_stage():
    run_id = _record()
    for _ in range(3):
        runs._touch_stage(run_id, "n2_enrichment", "done")
    assert stage_of(run_id, "researching")["leads_out"] == 3


def test_an_unknown_node_name_is_ignored_rather_than_raising():
    """A progress observer must never be able to break a run."""
    run_id = _record()
    runs._touch_stage(run_id, "n99_invented", "done")
    assert all(s["status"] == "waiting" for s in runs._runs[run_id]["stages"])


# --------------------------------------------------------------------------- #
# The sink itself
# --------------------------------------------------------------------------- #

def test_progress_is_a_no_op_until_something_installs_a_sink():
    """The command line and the tests run with no sink and must not care."""
    assert progress.active() is False
    progress.emit("n1_discovery", {"outcome": "done"})  # must not raise


def test_a_sink_that_throws_cannot_break_the_run():
    def broken(node, payload):
        raise RuntimeError("observer exploded")

    progress.install(broken)
    progress.emit("n1_discovery", {"outcome": "done"})  # swallowed
    progress.install(None)


def test_the_node_decorator_reports_through_the_sink():
    """
    The wiring itself, end to end: a decorated function reports when it runs.

    Without this the map above could be perfect and still never be consulted.
    """
    from src.reliability import node

    seen: list[tuple[str, str]] = []
    progress.install(lambda name, payload: seen.append((name, payload["outcome"])))

    @node("n1_discovery")
    def a_node(state):
        return {"company_name": "x"}

    a_node({"lead_id": "L1", "company_name": "Acme"})
    progress.install(None)

    assert seen == [("n1_discovery", "done")]


def test_a_node_that_fails_still_reports():
    from src.reliability import node

    seen: list[tuple[str, str]] = []
    progress.install(lambda name, payload: seen.append((name, payload["outcome"])))

    @node("n4_personalization")
    def explodes(state):
        raise ValueError("nope")

    result = explodes({"lead_id": "L1", "errors": []})
    progress.install(None)

    # The node's own contract is unchanged: the lead is flagged, not raised.
    assert result["needs_manual_review"] is True
    assert seen == [("n4_personalization", "failed")]


# --------------------------------------------------------------------------- #
# One at a time
# --------------------------------------------------------------------------- #

def test_a_second_run_is_refused_while_one_is_going():
    """
    Metered providers on free tiers. Two concurrent batches spend twice as
    fast, and neither one's progress can be followed.
    """
    run_id = _record()
    runs._active_run_id = run_id
    with pytest.raises(runs.RunBusy):
        runs.start("t", {"niche_ids": ["x"], "regions": ["UK"]}, "prompt")


def test_a_run_that_has_finished_does_not_block_the_next_one(monkeypatch):
    run_id = _record()
    runs._runs[run_id]["status"] = "completed"
    runs._active_run_id = run_id

    # Nothing should actually execute; only the refusal logic is under test.
    monkeypatch.setattr(runs.threading, "Thread", lambda **kw: type(
        "Fake", (), {"start": lambda self: None}
    )())
    record = runs.start("t", {"niche_ids": ["x"], "regions": ["UK"]}, "prompt")
    assert record["status"] == "running"
    assert record["stages"][0]["status"] == "running"
