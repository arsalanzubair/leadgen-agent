"""
N9 CRM and end-to-end graph tests (Build steps 9 and 10).

These run the REAL compiled graph over the dry-run fixtures, with no network
access and no API keys, which is exactly what `--dry-run` is for. If the wiring
in graph.py ever disagrees with Section 5, these fail.
"""

from __future__ import annotations

import csv

import pytest

from src.cli.run_batch import run_batch
from src.graph import (
    build_graph,
    checkpointer_for,
    render_mermaid,
    route_after_email,
    route_after_enrichment,
    thread_config,
)
from src.integrations import llm
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n9_crm_analytics import n9_crm_analytics, outcome_of, summarise
from src.settings import checkpoint_path
from src.state import CRM_COLUMNS, new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """
    Every test gets its own .data tree, no API keys, and the mock LLM. Nothing
    here can reach the network or a real mailbox.
    """
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("CHECKPOINT_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("SUPPRESSION_BACKEND", "local")
    monkeypatch.setenv("GLOBAL_DRY_RUN", "true")
    for key in ("GROQ_API_KEY", "GOOGLE_API_KEY", "DEEPL_API_KEY", "HUNTER_API_KEY",
                "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "BREVO_API_KEY",
                "GOOGLE_SHEETS_SPREADSHEET_ID", "AIRTABLE_API_KEY",
                "GOOGLE_PLACES_API_KEY", "APOLLO_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm, "provider_available", lambda name: name == "mock")
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


# =========================================================================== #
# Graph construction
# =========================================================================== #

def test_the_graph_compiles():
    assert build_graph(EXAMPLE) is not None


def test_every_section_5_node_is_present():
    nodes = set(build_graph(EXAMPLE).get_graph().nodes)
    for expected in (
        "config_load", "discovery", "enrichment", "qualification",
        "channel_selection", "personalization", "human_approval",
        "suppression_gate", "email_outreach", "linkedin_outreach",
        "reply_monitoring", "sequencer", "crm",
    ):
        assert expected in nodes, f"{expected} is missing from the graph"


def test_the_section_5_edges_exist():
    mermaid = render_mermaid(EXAMPLE)
    for edge in (
        "__start__ --> config_load",
        "config_load --> discovery",
        "discovery --> enrichment",
        "crm --> __end__",
    ):
        assert edge in mermaid, f"missing edge: {edge}"

    for source, target in (
        ("enrichment", "qualification"),
        ("qualification", "channel_selection"),
        ("channel_selection", "personalization"),
        ("personalization", "human_approval"),
        ("human_approval", "suppression_gate"),
        ("suppression_gate", "email_outreach"),
        ("suppression_gate", "linkedin_outreach"),
        ("email_outreach", "linkedin_outreach"),
        ("email_outreach", "reply_monitoring"),
        ("linkedin_outreach", "reply_monitoring"),
        ("reply_monitoring", "sequencer"),
        ("sequencer", "personalization"),      # the N8 -> N4 loop
        ("sequencer", "crm"),
    ):
        assert f"{source} -.-> {target}" in mermaid, f"missing edge {source}->{target}"


def test_crm_is_the_only_exit():
    """No lead may leave the system without a CRM row explaining why."""
    mermaid = render_mermaid(EXAMPLE)
    exits = [line for line in mermaid.splitlines() if "__end__;" in line]
    assert len(exits) == 1
    assert "crm --> __end__" in exits[0]


def test_checkpoint_paths_are_per_tenant():
    assert checkpoint_path("tenant_a") != checkpoint_path("tenant_b")
    assert "tenant_a" in checkpoint_path("tenant_a").name


def test_thread_config_carries_the_tenant():
    cfg = thread_config("tenant_a", "tenant_a-abc")
    assert cfg["configurable"]["thread_id"] == "tenant_a-abc"
    assert cfg["configurable"]["tenant_id"] == "tenant_a"


def test_unreachable_leads_skip_straight_to_crm():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_salon", region="AU", company_name="Ivy"
    )
    state["unreachable"] = True
    assert route_after_enrichment(state) == "crm"


def test_a_both_channel_lead_goes_to_linkedin_after_the_email():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X",
        linkedin_url="https://linkedin.com/company/x",
    )
    state["channel"] = "both"
    assert route_after_email(state) == "linkedin_outreach"


def test_an_email_only_lead_goes_to_reply_monitoring():
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X"
    )
    state["channel"] = "email"
    assert route_after_email(state) == "reply_monitoring"


# =========================================================================== #
# End-to-end dry run
# =========================================================================== #

def test_a_dry_run_batch_completes():
    batch = run_batch(EXAMPLE, dry_run=True)
    assert batch["leads"], "the dry run produced no leads"
    assert batch["finished_at"] is not None
    assert batch["run_id"]


def test_a_dry_run_processes_the_whole_fixture_corpus():
    batch = run_batch(EXAMPLE, dry_run=True)
    names = {lead["company_name"] for lead in batch["leads"]}
    # 9 fixture leads, one of which is a deliberate duplicate -> 8 distinct.
    assert len(batch["leads"]) == 8
    assert "Atelier Coiffure Saint-Germain" in names
    assert "Peak Performance Fitness DMCC" in names


def test_the_dry_run_exercises_every_important_outcome():
    """
    The fixture corpus is built so one dry run covers the paths that matter.
    If a change collapses several of these into one, the fixture stopped being
    a useful regression net.
    """
    batch = run_batch(EXAMPLE, dry_run=True)
    outcomes = set(summarise(batch["leads"]))
    assert "unreachable" in outcomes            # Ivy & Oak: no email, no LinkedIn
    assert "below_threshold" in outcomes        # Grantly: no signals
    assert any(o.startswith("in_sequence") for o in outcomes)


def test_the_unreachable_fixture_lead_is_excluded():
    batch = run_batch(EXAMPLE, dry_run=True)
    ivy = next(l for l in batch["leads"] if "Ivy" in l["company_name"])
    assert ivy["unreachable"] is True
    assert ivy["archived"] is True
    assert not ivy.get("draft_message"), "an unreachable lead must never be drafted"


def test_the_no_signals_fixture_lead_does_not_get_generic_copy():
    """Grantly has empty signals; it must not produce a generic draft."""
    batch = run_batch(EXAMPLE, dry_run=True)
    grantly = next(l for l in batch["leads"] if "Grantly" in l["company_name"])
    assert grantly["archived"] or grantly["needs_manual_review"]
    assert not (grantly.get("draft_message") or {}).get("email")


def test_the_french_fixture_lead_is_localised():
    batch = run_batch(EXAMPLE, dry_run=True)
    atelier = next(
        l for l in batch["leads"] if "Atelier" in l["company_name"]
    )
    assert atelier["language"] == "fr"
    draft = atelier["draft_message"]
    assert draft["translated"] is True
    # The EU profile requires a legal-basis line; the French footer supplies it.
    assert "RGPD" in draft["email"]["body"]
    assert "plus recevoir" in draft["email"]["body"]


def test_the_fixture_duplicate_is_deduplicated():
    batch = run_batch(EXAMPLE, dry_run=True)
    bright = [l for l in batch["leads"] if "Bright" in l["company_name"]]
    assert len(bright) == 1


def test_a_dry_run_sends_nothing_and_queues_nothing(capsys):
    from src.nodes.n6b_linkedin_outreach import pending_items

    run_batch(EXAMPLE, dry_run=True)
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert "was NOT" in captured
    assert pending_items(EXAMPLE) == [], "a dry run must not queue real work"


def test_a_dry_run_does_not_write_the_dedupe_ledger():
    from src.nodes.n1_discovery import load_seen

    run_batch(EXAMPLE, dry_run=True)
    assert load_seen(EXAMPLE) == {}


def test_a_dry_run_spends_no_hunter_quota():
    from src.counters import hunter_counter

    hunter_counter().reset()
    run_batch(EXAMPLE, dry_run=True)
    assert hunter_counter().used == 0


def test_the_limit_flag_caps_the_batch():
    batch = run_batch(EXAMPLE, dry_run=True, limit=2)
    assert len(batch["leads"]) == 2


def test_filtering_by_niche_and_region():
    batch = run_batch(EXAMPLE, dry_run=True, niche="local_salon", region="EU")
    assert batch["leads"]
    assert {l["region"] for l in batch["leads"]} == {"EU"}
    assert {l["niche_id"] for l in batch["leads"]} == {"local_salon"}


def test_one_failing_lead_does_not_crash_the_batch(monkeypatch):
    """Section 8, at batch scope."""
    from src.nodes import n3_qualification as module

    calls = {"n": 0}
    original = module.n3_qualification

    def sometimes_explode(state, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("qualification blew up for this one lead")
        return original(state, **kwargs)

    monkeypatch.setattr("src.graph.n3_qualification", sometimes_explode)

    batch = run_batch(EXAMPLE, dry_run=True)
    assert len(batch["leads"]) == 8, "the other leads must still have been processed"
    assert batch["needs_manual_review"], "the failing lead should be flagged"


def test_a_bad_tenant_config_halts_before_discovery(monkeypatch, tmp_path):
    import yaml

    from src.reliability import ConfigError
    from src.settings import ROOT

    raw = yaml.safe_load(
        (ROOT / "config" / "tenants" / "example_tenant.yaml").read_text(encoding="utf-8")
    )
    raw.pop("tone")
    tenants = tmp_path / "tenants"
    tenants.mkdir()
    (tenants / f"{EXAMPLE}.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(
        "src.nodes.n0_config_load.tenant_config_path",
        lambda tid: tenants / f"{tid}.yaml",
    )

    def explode(*args, **kwargs):
        raise AssertionError("discovery must not run with an invalid config")

    monkeypatch.setattr("src.cli.run_batch.discover", explode)
    with pytest.raises(ConfigError, match="tone"):
        run_batch(EXAMPLE, dry_run=True)


# =========================================================================== #
# Checkpointing across runs
# =========================================================================== #

def test_leads_are_checkpointed_per_tenant():
    run_batch(EXAMPLE, dry_run=True, limit=2)
    assert checkpoint_path(EXAMPLE).exists()

    with checkpointer_for(EXAMPLE) as saver:
        threads = {
            cp.config.get("configurable", {}).get("thread_id")
            for cp in saver.list(None)
        }
    assert len(threads) >= 2
    assert all(str(t).startswith(f"{EXAMPLE}-") for t in threads if t)


def test_a_second_run_resumes_rather_than_duplicating():
    """
    The lead_id is deterministic, so a re-run lands on the SAME thread instead
    of creating a parallel one.
    """
    first = run_batch(EXAMPLE, dry_run=True, limit=2)
    ids_first = {l["lead_id"] for l in first["leads"]}

    second = run_batch(EXAMPLE, dry_run=True, limit=2)
    ids_second = {l["lead_id"] for l in second["leads"]}
    assert ids_first == ids_second


# =========================================================================== #
# N9 -- the CRM row
# =========================================================================== #

def test_the_crm_csv_has_the_looker_header_and_one_row_per_lead():
    batch = run_batch(EXAMPLE, dry_run=True)
    from src.settings import dry_run_output_path

    path = dry_run_output_path(EXAMPLE)
    assert path.exists()

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))

    assert rows[0] == list(CRM_COLUMNS), "the header must match CRM_COLUMNS exactly"
    assert len(rows) - 1 == len(batch["leads"]), "exactly one row per lead"
    lead_ids = [row[0] for row in rows[1:]]
    assert len(lead_ids) == len(set(lead_ids)), "no duplicate rows"


def test_crm_values_are_looker_friendly():
    run_batch(EXAMPLE, dry_run=True)
    from src.settings import dry_run_output_path

    with dry_run_output_path(EXAMPLE).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    for row in rows:
        assert row["unreachable"] in ("TRUE", "FALSE")
        assert row["archived"] in ("TRUE", "FALSE")
        if row["last_updated"]:
            assert row["last_updated"][4] == "-" and "T" in row["last_updated"]
        if row["fit_score"]:
            assert row["fit_score"].isdigit()


def test_upsert_updates_rather_than_appends():
    """Re-running a batch must not produce five rows for one company."""
    run_batch(EXAMPLE, dry_run=True, limit=3)
    from src.settings import dry_run_output_path

    path = dry_run_output_path(EXAMPLE)
    with path.open(encoding="utf-8") as handle:
        first_count = sum(1 for _ in handle)

    run_batch(EXAMPLE, dry_run=True, limit=3)
    with path.open(encoding="utf-8") as handle:
        second_count = sum(1 for _ in handle)

    assert first_count == second_count


def test_a_crm_write_failure_does_not_fail_the_lead(config, monkeypatch):
    """The work is done; only the record failed."""
    class Broken:
        def upsert_lead(self, state):
            raise RuntimeError("sheets down")

    monkeypatch.setattr("src.integrations.sheets_crm.get_backend", lambda *a, **k: Broken())
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X"
    )
    state["send_status"] = "sent"
    update = n9_crm_analytics(state, tenant_config=config)
    assert not update.get("needs_manual_review")
    assert update["errors"][-1]["node"] == "n9_crm_analytics"


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"needs_manual_review": True}, "needs_manual_review"),
        ({"reply_category": "interested"}, "interested_manual_handoff"),
        ({"unreachable": True}, "unreachable"),
        ({"suppression_status": "blocked_optout"}, "suppressed"),
        ({"suppression_status": "blocked_compliance"}, "blocked_compliance"),
        ({"archived": True, "archive_reason": "below_threshold"}, "below_threshold"),
        ({"send_status": "sent"}, "sent_awaiting_reply"),
        ({"send_status": "pending_manual_send"}, "awaiting_manual_linkedin_send"),
        ({"send_status": "rate_limited"}, "queued_for_next_run"),
    ],
)
def test_outcome_derivation(overrides, expected):
    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X"
    )
    state.update(overrides)
    assert outcome_of(state) == expected


def test_a_mid_cadence_lead_is_not_reported_as_unsent():
    """
    N8 resets approval_status and send_status when scheduling the next touch;
    reading only those would call a sent lead unsent.
    """
    from datetime import timedelta

    from src.state import utcnow

    state = new_lead_state(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US", company_name="X"
    )
    state["send_status"] = "not_sent"
    state["approval_status"] = "pending"
    state["last_touch_at"] = utcnow() - timedelta(days=1)
    state["next_touch_due"] = utcnow() + timedelta(days=3)
    state["sequence_step"] = 1
    assert outcome_of(state) == "in_sequence_touch_2_scheduled"


def test_summarise_counts_outcomes():
    batch = run_batch(EXAMPLE, dry_run=True)
    counts = summarise(batch["leads"])
    assert sum(counts.values()) == len(batch["leads"])
