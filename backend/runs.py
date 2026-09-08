"""
runs.py -- starting a search, and answering what it is doing.

`run_batch` is a blocking call that returns a finished batch, so this module
runs it on its own thread and keeps a record the HTTP layer can read while it
is still going. The record's shape is the dashboard's `AgentRun` exactly, so
the client needs no translation layer.

Two decisions worth knowing about:

  * One run at a time. A second start is refused rather than queued. These are
    metered providers on free tiers, and two concurrent batches spend twice as
    fast while making the progress of either impossible to follow.

  * Records are written to disk as they change. A run that was going when the
    service restarted would otherwise vanish, and the run is where the leads
    are -- losing the record loses the results.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from src import progress
from src.settings import ROOT, _assert_safe_tenant_id
from src.state import utcnow

# --------------------------------------------------------------------------- #
# Naming: the graph's nodes, in the words the dashboard uses
# --------------------------------------------------------------------------- #

# Keyed by the name each node passes to `@node(...)`, which is what arrives at
# the progress sink -- NOT the shorter labels graph.py uses for its edges. The
# two differ, and using the wrong set produces a screen where every stage
# reports "skipped" while the run works perfectly. `check_stage_coverage`
# below exists so that mistake cannot be made silently a second time.
#
# The dashboard's RunStage list was written against these nodes, so this is a
# rename and not a re-grouping: nothing is lost in translation.
STAGE_FOR_NODE: dict[str, str] = {
    "n0_config_load": "understanding",
    "n1_discovery": "finding",
    "n2_enrichment": "researching",
    "n3_qualification": "matching",
    "n3_5_channel_selection": "choosing_channel",
    "n4_personalization": "drafting",
    "n5_human_approval": "reviewing",
    "n5_5_suppression_gate": "checking_rules",
    "n6a_email_outreach": "sending_email",
    "n6b_linkedin_outreach": "linkedin_prep",
    "n7_reply_monitoring": "watching_replies",
    "n8_followup_sequencer": "following_up",
    "n9_crm_analytics": "saving",
}


# The order the dashboard renders them in.
RUN_STAGES: list[str] = [
    "understanding", "finding", "researching", "matching", "choosing_channel",
    "drafting", "reviewing", "checking_rules", "sending_email",
    "linkedin_prep", "watching_replies", "following_up", "saving",
]

NOTES: dict[str, str] = {
    "understanding": "Working out where to search",
    "finding": "Looking for businesses",
    "researching": "Reading their websites",
    "matching": "Checking how well they match",
    "choosing_channel": "Deciding how to reach them",
    "drafting": "Writing the outreach",
    "reviewing": "Waiting for your approval",
    "checking_rules": "Applying your sending rules",
    "sending_email": "Sending email",
    "linkedin_prep": "Preparing LinkedIn messages for you to send",
    "watching_replies": "Watching for replies",
    "following_up": "Scheduling follow-ups",
    "saving": "Saving to your records",
}


def _blank_stages() -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "status": "waiting",
            "duration_ms": None,
            "leads_in": 0,
            "leads_out": 0,
            "leads_diverted": 0,
            "started_at": None,
            "finished_at": None,
            "note": "",
        }
        for stage in RUN_STAGES
    ]


def check_stage_coverage() -> tuple[set[str], set[str]]:
    """
    Which nodes report into no stage, and which stages no node feeds.

    Both sets empty is the healthy state. Called by the tests; a rename in
    src/nodes/ that is not mirrored here fails there rather than turning the
    progress display into a row of skipped steps in front of the user.
    """
    import pkgutil

    from src import nodes as nodes_pkg

    declared: set[str] = set()
    for info in pkgutil.iter_modules(nodes_pkg.__path__):
        module = __import__(f"src.nodes.{info.name}", fromlist=["*"])
        for value in vars(module).values():
            name = getattr(value, "__node_name__", None)
            if name:
                declared.add(name)

    unmapped = declared - set(STAGE_FOR_NODE)
    unfed = set(RUN_STAGES) - set(STAGE_FOR_NODE.values())
    return unmapped, unfed


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_runs: dict[str, dict[str, Any]] = {}
_active_run_id: str = ""


class RunBusy(Exception):
    """A run is already going. Raised instead of queueing a second one."""


def runs_path(tenant_id: str) -> Path:
    _assert_safe_tenant_id(tenant_id)
    return ROOT / "config" / "tenants" / tenant_id / "runs.json"


def _persist(tenant_id: str) -> None:
    """Write this tenant's runs to disk. Best effort: never breaks a run."""
    try:
        path = runs_path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            mine = [r for r in _runs.values() if r["tenant_id"] == tenant_id]
        payload = json.dumps(mine, default=str, indent=2)
        path.write_text(payload, encoding="utf-8")
    except Exception:  # noqa: BLE001 -- a lost record must not fail the batch
        pass


def _load(tenant_id: str) -> None:
    """Read records written by an earlier process, once."""
    path = runs_path(tenant_id)
    if not path.exists():
        return
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(stored, list):
        return
    with _lock:
        for record in stored:
            run_id = record.get("run_id")
            if not run_id or run_id in _runs:
                continue
            # Nothing is running in this process, so a record that claims to be
            # is a run the previous process took with it.
            if record.get("status") in ("running", "queued"):
                record["status"] = "failed"
                record["finished_at"] = record.get("finished_at") or utcnow()
                record["error"] = "The service stopped while this was running."
            _runs[run_id] = record


_loaded: set[str] = set()


def _ensure_loaded(tenant_id: str) -> None:
    if tenant_id in _loaded:
        return
    _loaded.add(tenant_id)
    _load(tenant_id)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def list_runs(tenant_id: str) -> list[dict[str, Any]]:
    _ensure_loaded(tenant_id)
    with _lock:
        mine = [dict(r) for r in _runs.values() if r["tenant_id"] == tenant_id]
    return sorted(mine, key=lambda r: r["started_at"], reverse=True)


def get_run(tenant_id: str, run_id: str) -> dict[str, Any] | None:
    _ensure_loaded(tenant_id)
    with _lock:
        record = _runs.get(run_id)
        if not record or record["tenant_id"] != tenant_id:
            return None
        return dict(record)


def leads_from_runs(tenant_id: str) -> list[dict[str, Any]]:
    """
    Every lead this workspace's runs have produced, newest run first.

    The runs hold the lead records themselves, so the leads screen has a real
    source without a second store to keep in step with them.
    """
    seen: set[str] = set()
    leads: list[dict[str, Any]] = []
    for record in list_runs(tenant_id):
        for lead in record.get("leads", []):
            lead_id = lead.get("lead_id")
            if not lead_id or lead_id in seen:
                continue
            seen.add(lead_id)
            leads.append(lead)
    return leads


def active_run_id() -> str:
    with _lock:
        return _active_run_id


# --------------------------------------------------------------------------- #
# Starting one
# --------------------------------------------------------------------------- #

def start(tenant_id: str, config: dict[str, Any], prompt: str) -> dict[str, Any]:
    """
    Begin a run and return its record immediately.

    The work happens on a worker thread. The caller gets a record whose first
    stage is already marked running, so the screen has something true to show
    before any node has finished.
    """
    global _active_run_id
    _ensure_loaded(tenant_id)

    with _lock:
        if _active_run_id and _runs.get(_active_run_id, {}).get("status") == "running":
            raise RunBusy(
                "A search is already running. Wait for it to finish before "
                "starting another."
            )

    run_id = uuid.uuid4().hex[:12]
    niche_ids = [str(n) for n in config.get("niche_ids", []) if str(n).strip()]
    regions = [str(r) for r in config.get("regions", []) if str(r).strip()]
    language_map = config.get("language_map") or {}

    stages = _blank_stages()
    stages[0]["status"] = "running"
    stages[0]["started_at"] = utcnow()
    stages[0]["note"] = NOTES["understanding"]

    record: dict[str, Any] = {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "status": "running",
        "dry_run": bool(config.get("dry_run", True)),
        "started_at": utcnow(),
        "finished_at": None,
        "targets": [
            {"niche_id": n, "region": r, "language": language_map.get(r, "en")}
            for n in niche_ids
            for r in regions
        ],
        "low_yield_targets": [],
        "leads_discovered": 0,
        "leads_qualified": 0,
        "lead_ids": [],
        "needs_manual_review": [],
        "archived": [],
        "stages": stages,
        "prompt": prompt,
        "config": config,
        "leads": [],
        "error": "",
    }

    with _lock:
        _runs[run_id] = record
        _active_run_id = run_id
    _persist(tenant_id)

    worker = threading.Thread(
        target=_execute,
        args=(tenant_id, run_id, config),
        name=f"leadgen-run-{run_id}",
        daemon=True,
    )
    worker.start()
    return dict(record)


def _touch_stage(run_id: str, node_name: str, outcome: str) -> None:
    """Fold one node's completion into the run's stage list."""
    stage_name = STAGE_FOR_NODE.get(node_name)
    if not stage_name:
        return
    now = utcnow()
    with _lock:
        record = _runs.get(run_id)
        if not record:
            return
        stages = record["stages"]
        index = RUN_STAGES.index(stage_name)
        stage = stages[index]

        if stage["started_at"] is None:
            stage["started_at"] = now
        stage["note"] = NOTES.get(stage_name, "")
        stage["leads_in"] += 1
        if outcome == "done":
            stage["leads_out"] += 1
            stage["status"] = "completed"
            stage["finished_at"] = now
        elif outcome == "diverted":
            stage["leads_diverted"] += 1
            stage["status"] = "completed"
            stage["finished_at"] = now
        elif outcome == "paused":
            stage["status"] = "running"
        elif outcome == "failed":
            stage["leads_diverted"] += 1
            stage["status"] = "failed"
            stage["finished_at"] = now

        # Anything earlier that never reported was routed around, not pending.
        for earlier in stages[:index]:
            if earlier["status"] == "waiting":
                earlier["status"] = "skipped"
        # Show the next step as the one being worked on.
        for later in stages[index + 1:]:
            if later["status"] == "waiting":
                later["status"] = "running"
                later["started_at"] = later["started_at"] or now
                later["note"] = NOTES.get(later["stage"], "")
                break


def _execute(tenant_id: str, run_id: str, config: dict[str, Any]) -> None:
    """The worker thread: run the batch, recording progress as it goes."""
    global _active_run_id

    # Imported here rather than at module scope: this pulls in the whole graph
    # and every provider, which the settings service should not pay for just to
    # answer a request about connections.
    from src.cli.run_batch import run_batch
    from src.reliability import ConfigError

    niche_ids = [str(n) for n in config.get("niche_ids", []) if str(n).strip()]
    regions = [str(r) for r in config.get("regions", []) if str(r).strip()]

    progress.install(lambda node, payload: _touch_stage(run_id, node, payload.get("outcome", "done")))
    error = ""
    batch: dict[str, Any] | None = None
    try:
        batch = run_batch(  # type: ignore[assignment]
            tenant_id,
            dry_run=bool(config.get("dry_run", True)),
            limit=int(config.get("max_leads_per_run") or 0),
            resume=True,
            # run_batch filters by a single niche and region; an empty string
            # means "every one this workspace has configured".
            niche=niche_ids[0] if len(niche_ids) == 1 else "",
            region=regions[0] if len(regions) == 1 else "",
        )
    except ConfigError as exc:
        error = str(exc)
    except BaseException as exc:  # noqa: BLE001 -- the record must always close
        error = f"{exc.__class__.__name__}: {exc}"
    finally:
        progress.install(None)

    now = utcnow()
    with _lock:
        record = _runs.get(run_id)
        if record is not None:
            leads = list(batch.get("leads", [])) if batch else []
            qualified = [
                lead for lead in leads
                if not lead.get("archived") and int(lead.get("fit_score") or 0) > 0
            ]
            record["finished_at"] = now
            record["status"] = "failed" if error else "completed"
            record["error"] = error
            if batch:
                record["low_yield_targets"] = batch.get("low_yield_targets", [])
                record["needs_manual_review"] = batch.get("needs_manual_review", [])
                record["archived"] = batch.get("archived", [])
                record["targets"] = batch.get("targets", record["targets"])
                # Round-tripped through JSON so the record holds only what the
                # client can actually receive -- datetimes become strings here
                # rather than at every read.
                record["leads"] = json.loads(json.dumps(leads, default=str))
                record["lead_ids"] = [
                    lead.get("lead_id", "") for lead in record["leads"]
                ]
                record["leads_discovered"] = len(leads)
                record["leads_qualified"] = len(qualified)
            for stage in record["stages"]:
                if stage["status"] in ("waiting", "running"):
                    stage["status"] = "failed" if error else (
                        "skipped" if stage["started_at"] is None else "completed"
                    )
                    stage["finished_at"] = stage["finished_at"] or now
        if _active_run_id == run_id:
            _active_run_id = ""
    _persist(tenant_id)


def __reset_for_tests() -> None:
    """Clear the registry. Tests only."""
    global _active_run_id
    with _lock:
        _runs.clear()
        _active_run_id = ""
    _loaded.clear()
