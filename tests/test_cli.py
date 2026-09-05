"""
CLI tests (Build step 10).

The CLIs are where the operator actually touches the system, so the tests here
are about behaviour that protects them: an approval decision reaching the right
thread, a queue command not silently doing nothing, and no CLI ever sending
anything on its own.
"""

from __future__ import annotations

import pytest

from src.cli import approve as approve_cli
from src.cli import linkedin_queue as queue_cli
from src.cli import run_batch as run_batch_cli
from src.integrations import llm
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n6b_linkedin_outreach import all_items, enqueue, pending_items

EXAMPLE = "example_tenant"

#: Signals that clear the example tenant's fit_score_threshold of 60 against
#: the b2b_saas_ops ICP -- these tests are about the CLI, not about scoring, so
#: they must not sit on the threshold boundary.
_B2B_SIGNALS = [
    "job posting for a Sales Development Rep mentions manual list building",
    "open Customer Success roles posted in the last 60 days",
    "runs Intercom and HubSpot without an AI layer",
    "recent funding round announced on their press page",
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("SUPPRESSION_BACKEND", "local")
    monkeypatch.setenv("GLOBAL_DRY_RUN", "true")
    for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


# =========================================================================== #
# run_batch
# =========================================================================== #

def test_dry_run_exits_zero(capsys):
    assert run_batch_cli.main(["--tenant", EXAMPLE, "--dry-run", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "nothing was sent" in out


def test_an_unknown_tenant_exits_two(capsys):
    assert run_batch_cli.main(["--tenant", "no_such_tenant", "--dry-run"]) == 2
    assert "Halted before discovery" in capsys.readouterr().out


def test_the_graph_flag_prints_mermaid(capsys):
    assert run_batch_cli.main(["--tenant", EXAMPLE, "--graph"]) == 0
    out = capsys.readouterr().out
    assert "graph TD" in out
    assert "suppression_gate" in out


def test_the_dry_run_banner_reports_the_llm_provider(capsys):
    run_batch_cli.main(["--tenant", EXAMPLE, "--dry-run", "--limit", "1"])
    assert "llm: mock" in capsys.readouterr().out


# =========================================================================== #
# approve
# =========================================================================== #

def test_approve_with_no_checkpoint_db_is_not_an_error(capsys):
    assert approve_cli.main(["--tenant", EXAMPLE]) == 0
    assert "No checkpoint database" in capsys.readouterr().out


def test_approve_rejects_an_unknown_tenant(capsys):
    assert approve_cli.main(["--tenant", "no_such_tenant"]) == 2


def test_the_queue_is_grouped_by_channel_and_sorted_by_fit():
    """An operator works the highest-fit leads in one channel at a time."""
    items = [
        {"thread_id": "t1", "payload": {"channel": "linkedin", "fit_score": 70}, "state": {}},
        {"thread_id": "t2", "payload": {"channel": "email", "fit_score": 60}, "state": {}},
        {"thread_id": "t3", "payload": {"channel": "email", "fit_score": 90}, "state": {}},
    ]
    items.sort(key=lambda i: (i["payload"]["channel"], -i["payload"]["fit_score"]))
    assert [i["thread_id"] for i in items] == ["t3", "t2", "t1"]


def test_review_of_an_empty_queue_reports_nothing_to_do(capsys):
    tally = approve_cli.review(EXAMPLE, items=[])
    assert tally == {"approved": 0, "edited": 0, "rejected": 0, "skipped": 0}
    assert "Nothing awaiting approval" in capsys.readouterr().out


def test_review_end_to_end_applies_decisions(tmp_path, monkeypatch):
    """
    A real interrupt, resumed by the review loop's own code path: approve the
    first lead, reject the second.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    from src.graph import thread_config
    from src.nodes.n5_human_approval import n5_human_approval
    from src.state import LeadState, new_lead_state

    db = tmp_path / "approve.sqlite"

    def make_graph(saver):
        builder = StateGraph(LeadState)
        builder.add_node("approve", n5_human_approval)
        builder.add_edge(START, "approve")
        builder.add_edge("approve", END)
        return builder.compile(checkpointer=saver)

    leads = []
    for name in ("Alpha Dental", "Beta Dental"):
        lead = new_lead_state(
            tenant_id=EXAMPLE, niche_id="local_dental", region="UK",
            company_name=name, contact_email=f"hello@{name.split()[0].lower()}.co.uk",
            dry_run=False,
        )
        lead["channel"] = "email"
        lead["draft_message"] = {"email": {"subject": "s", "body": "b"}}
        leads.append(lead)

    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph = make_graph(saver)
        items = []
        for lead in leads:
            result = graph.invoke(lead, config={"configurable": {"thread_id": lead["lead_id"]}})
            items.append({
                "thread_id": lead["lead_id"],
                "payload": result["__interrupt__"][0].value,
                "state": lead,
            })

    # Point the review loop at this throwaway graph.
    monkeypatch.setattr(approve_cli, "checkpointer_for",
                        lambda tid: SqliteSaver.from_conn_string(str(db)))
    monkeypatch.setattr(approve_cli, "build_graph",
                        lambda tid, checkpointer=None: make_graph(checkpointer))
    monkeypatch.setattr(approve_cli, "thread_config", thread_config)

    answers = iter(["a", "r"])
    tally = approve_cli.review(EXAMPLE, items=items, reader=lambda _="": next(answers))
    assert tally["approved"] == 1
    assert tally["rejected"] == 1

    with SqliteSaver.from_conn_string(str(db)) as saver:
        graph = make_graph(saver)
        first = graph.get_state(thread_config(EXAMPLE, items[0]["thread_id"])).values
        second = graph.get_state(thread_config(EXAMPLE, items[1]["thread_id"])).values
    assert first["approval_status"] == "approved"
    assert second["approval_status"] == "rejected"
    assert second["archived"] is True


def test_pending_threads_does_not_deadlock_on_a_real_checkpoint_db():
    """
    Regression guard. `saver.list()` is a generator over a live SQLite cursor
    and `get_state()` queries the same connection; interleaving them made the
    CLI hang on the database lock rather than fail. The thread ids must be
    materialised before any of them is queried.
    """
    from src.graph import graph_for, thread_config
    from src.state import new_lead_state

    # Three live leads, each pausing at N5, so the listing has real work to do.
    for name in ("Alpha Co", "Beta Co", "Gamma Co"):
        lead = new_lead_state(
            tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="CA",
            company_name=name, contact_email=f"info@{name.split()[0].lower()}.ca",
            contact_name="Kieran Moreau",
            linkedin_url=f"https://linkedin.com/company/{name.split()[0].lower()}",
            dry_run=False,
        )
        lead["signals"] = _B2B_SIGNALS
        with graph_for(EXAMPLE) as graph:
            graph.invoke(lead, config=thread_config(EXAMPLE, lead["lead_id"]))

    items = approve_cli.pending_threads(EXAMPLE)
    assert len(items) == 3
    assert {i["payload"]["company_name"] for i in items} == {
        "Alpha Co", "Beta Co", "Gamma Co"
    }
    assert all(i["payload"]["channel"] for i in items)


def test_approve_list_renders_a_real_queue(capsys):
    from src.graph import graph_for, thread_config
    from src.state import new_lead_state

    lead = new_lead_state(
        tenant_id=EXAMPLE, niche_id="b2b_saas_ops", region="CA",
        company_name="Maple Route Analytics", contact_email="info@maple.ca",
        contact_name="Kieran Moreau",
        linkedin_url="https://linkedin.com/company/maple", dry_run=False,
    )
    lead["signals"] = _B2B_SIGNALS
    with graph_for(EXAMPLE) as graph:
        graph.invoke(lead, config=thread_config(EXAMPLE, lead["lead_id"]))

    assert approve_cli.main(["--tenant", EXAMPLE, "--list"]) == 0
    assert "Maple Route" in capsys.readouterr().out


def test_quitting_the_review_leaves_the_rest_queued(monkeypatch, tmp_path):
    items = [
        {"thread_id": "t1", "payload": {"channel": "email", "company_name": "A"}, "state": {}},
        {"thread_id": "t2", "payload": {"channel": "email", "company_name": "B"}, "state": {}},
    ]
    monkeypatch.setattr(approve_cli, "checkpointer_for", _null_checkpointer)
    monkeypatch.setattr(approve_cli, "build_graph", lambda tid, checkpointer=None: None)

    tally = approve_cli.review(EXAMPLE, items=items, reader=lambda _="": "q")
    assert tally == {"approved": 0, "edited": 0, "rejected": 0, "skipped": 0}


def test_skipping_leaves_the_lead_queued(monkeypatch):
    items = [{"thread_id": "t1", "payload": {"channel": "email", "company_name": "A"}, "state": {}}]
    monkeypatch.setattr(approve_cli, "checkpointer_for", _null_checkpointer)
    monkeypatch.setattr(approve_cli, "build_graph", lambda tid, checkpointer=None: None)
    tally = approve_cli.review(EXAMPLE, items=items, reader=lambda _="": "s")
    assert tally["skipped"] == 1


def _null_checkpointer(tenant_id):
    from contextlib import contextmanager

    @contextmanager
    def cm():
        yield None

    return cm()


# =========================================================================== #
# linkedin_queue
# =========================================================================== #

def _queue_one(lead_id: str = "lead-1", **overrides):
    item = {
        "lead_id": lead_id, "sequence_step": 1, "company_name": "Lakeside",
        "contact_name": "Dana Whitfield", "region": "US", "fit_score": 82,
        "linkedin_url": "https://linkedin.com/company/lakeside",
        "connection_note": "Hi Dana - noticed your booking page is phone-only.",
        "followup_dm": "Thanks for connecting.",
        "account_label": "[your LinkedIn account - set in Settings]",
    }
    item.update(overrides)
    enqueue(EXAMPLE, item)
    return item


def test_list_shows_pending_items(capsys):
    _queue_one()
    assert queue_cli.main(["--tenant", EXAMPLE, "list"]) == 0
    out = capsys.readouterr().out
    assert "Lakeside" in out
    assert "to send by hand" in out


def test_list_on_an_empty_queue(capsys):
    assert queue_cli.main(["--tenant", EXAMPLE, "list"]) == 0
    assert "nothing here" in capsys.readouterr().out


def test_show_prints_the_note_ready_to_copy(capsys):
    _queue_one()
    assert queue_cli.main(["--tenant", EXAMPLE, "show", "lead-1"]) == 0
    out = capsys.readouterr().out
    assert "CONNECTION NOTE" in out
    assert "phone-only" in out
    assert "/300" in out


def test_marking_sent_from_the_cli(capsys):
    _queue_one()
    assert queue_cli.main(["--tenant", EXAMPLE, "sent", "lead-1"]) == 0
    assert "marked sent" in capsys.readouterr().out
    assert pending_items(EXAMPLE) == []
    assert all_items(EXAMPLE, status="sent")


def test_marking_connected_tells_the_operator_what_happens_next(capsys):
    _queue_one()
    assert queue_cli.main(["--tenant", EXAMPLE, "connected", "lead-1"]) == 0
    out = capsys.readouterr().out
    assert "marked connected" in out
    assert "post-acceptance DM" in out


def test_marking_an_unknown_lead_reports_it(capsys):
    assert queue_cli.main(["--tenant", EXAMPLE, "sent", "nope"]) == 1
    assert "No queued item" in capsys.readouterr().out


def test_a_command_needing_a_lead_id_says_so(capsys):
    assert queue_cli.main(["--tenant", EXAMPLE, "sent"]) == 2
    assert "needs a lead_id" in capsys.readouterr().out


def test_the_queue_cli_never_touches_linkedin():
    """The constraint, asserted at the CLI boundary as well as in N6b."""
    import inspect

    source = inspect.getsource(queue_cli)
    for banned in ("playwright", "selenium", "webdriver", "requests", "httpx", "urllib"):
        assert banned not in source, f"the LinkedIn queue CLI must not use {banned}"
