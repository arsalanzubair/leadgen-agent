"""
run_batch.py -- main entrypoint, runs one tenant batch.

    python -m src.cli.run_batch --tenant example_tenant --dry-run
    python -m src.cli.run_batch --tenant example_tenant --limit 5
    python -m src.cli.run_batch --tenant example_tenant --resume
    python -m src.cli.run_batch --tenant example_tenant --graph

--dry-run runs the FULL graph against tests/fixtures/sample_leads.json instead
of live discovery, and NEVER sends email or queues a real LinkedIn item: N6a
and N6b print what would happen. This is the default way to test a change
before pointing the system at a real tenant.

WHAT A RUN ACTUALLY DOES
------------------------
1. N0 loads and validates the tenant config, resolving (niche, region) targets.
   A missing required field halts here, before anything touches the outside
   world or spends a quota.
2. N1 discovers and deduplicates at BATCH level -- one target produces many
   leads, which a per-lead graph node cannot do.
3. N2's Hunter top-N is chosen at batch level too, because "top 5 of this
   batch" is inherently comparative.
4. Each lead then gets its own graph thread, keyed by lead_id. One lead's
   failure, interrupt or archive cannot affect another's (Section 8).
5. `--resume` picks up existing checkpointed threads whose next touch is due,
   which is what a scheduled run does day to day.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import Any, Iterable

from rich.console import Console
from rich.table import Table

from src.graph import build_graph, checkpointer_for, thread_config
from src.integrations import llm
from src.integrations.langsmith_setup import configure as configure_tracing
from src.integrations.langsmith_setup import run_config
from src.nodes.n0_config_load import load_tenant_config, resolve_batch_targets
from src.nodes.n1_discovery import discover
from src.nodes.n2_enrichment import select_lookup_candidates
from src.providers import enrichment_for
from src.nodes.n9_crm_analytics import outcome_of, summarise
from src.reliability import ConfigError, log
from src.settings import checkpoint_path, global_dry_run
from src.state import LeadBatch, LeadState, utcnow

console = Console()


# --------------------------------------------------------------------------- #
# Running one lead
# --------------------------------------------------------------------------- #

def run_lead(graph, tenant_id: str, lead: LeadState, run_id: str) -> LeadState:
    """
    Push one lead through the graph on its own thread.

    An interrupt (N5 awaiting approval) is a normal outcome, not an error: the
    thread stays checkpointed and cli/approve.py resumes it later.
    """
    config = thread_config(tenant_id, lead["lead_id"])
    config.update(run_config(lead, run_id=run_id))
    try:
        result = graph.invoke(lead, config=config)
        if "__interrupt__" in result:
            log.info(
                "lead %s (%s) paused for approval",
                lead["lead_id"], lead.get("company_name"),
            )
        return {**lead, **{k: v for k, v in result.items() if k != "__interrupt__"}}
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        # @node already catches per-node failures; this is the last net for a
        # failure in the runtime itself. One lead must never take down a batch.
        log.error(
            "lead %s (%s) failed outside any node: %s",
            lead.get("lead_id"), lead.get("company_name"), exc,
        )
        return {
            **lead,
            "needs_manual_review": True,
            "manual_review_reason": f"graph invocation failed: {exc}",
        }


def resume_due_threads(graph, tenant_id: str, run_id: str) -> list[LeadState]:
    """
    Resume checkpointed leads whose next touch has come due.

    This is what a scheduled run does for the book of work already in flight,
    as opposed to newly discovered leads.
    """
    from src.nodes.n8_followup_sequencer import is_touch_due

    resumed: list[LeadState] = []

    # The checkpointer is the source of truth for which threads exist -- there
    # is no side table of in-flight leads that could disagree with it.
    #
    # The thread ids are materialised BEFORE any is queried: `saver.list()` is a
    # generator over a live SQLite cursor and `get_state()` queries the same
    # connection, so interleaving them deadlocks on the database lock.
    with checkpointer_for(tenant_id) as saver:
        thread_ids = list(dict.fromkeys(
            checkpoint.config.get("configurable", {}).get("thread_id", "")
            for checkpoint in saver.list(None)
        ))

    for thread_id in thread_ids:
        if not thread_id:
            continue
        snapshot = graph.get_state(thread_config(tenant_id, thread_id))
        state = snapshot.values
        if not state or snapshot.interrupts:
            continue                      # awaiting approval, not a due touch
        if state.get("archived"):
            continue
        if not is_touch_due(state):
            continue
        log.info(
            "resuming lead=%s (%s): touch due %s",
            thread_id, state.get("company_name"), state.get("next_touch_due"),
        )
        resumed.append(run_lead(graph, tenant_id, state, run_id))

    return resumed


# --------------------------------------------------------------------------- #
# Running a batch
# --------------------------------------------------------------------------- #

def run_batch(
    tenant_id: str,
    *,
    dry_run: bool = False,
    limit: int = 0,
    resume: bool = True,
    niche: str = "",
    region: str = "",
) -> LeadBatch:
    """Discover, enrich-select, and run every lead. Returns the batch record."""
    run_id = uuid.uuid4().hex[:12]
    tracing = configure_tracing()

    config = load_tenant_config(tenant_id)
    targets = resolve_batch_targets(config)
    if niche:
        targets = [t for t in targets if t["niche_id"] == niche]
    if region:
        targets = [t for t in targets if t["region"] == region]
    if not targets:
        raise ConfigError(
            f"no targets left after filtering (niche={niche!r}, region={region!r})"
        )

    console.rule(f"[bold]{tenant_id}[/bold] - run {run_id}")
    console.print(
        f"mode: [bold]{'DRY RUN' if dry_run else 'LIVE'}[/bold]   "
        f"targets: {len(targets)}   llm: {llm.active_provider()}   "
        f"tracing: {'on' if tracing else 'off'}"
    )
    if not dry_run and global_dry_run():
        console.print(
            "[yellow]GLOBAL_DRY_RUN is true in .env: N6a/N6b will print instead "
            "of sending, whatever this flag says.[/yellow]"
        )

    batch: LeadBatch = {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "started_at": utcnow(),
        "finished_at": None,
        "dry_run": dry_run,
        "targets": targets,
        "leads": [],
        "low_yield_targets": [],
        "needs_manual_review": [],
        "archived": [],
        "errors": [],
    }

    # -- N1: batch-level discovery + dedupe -------------------------------- #
    leads, low_yield = discover(config, targets, dry_run=dry_run)
    batch["low_yield_targets"] = low_yield
    if limit:
        leads = leads[:limit]

    if low_yield:
        console.print(
            "[yellow]low_yield:[/yellow] "
            + ", ".join(f"{t['niche_id']}/{t['region']}" for t in low_yield)
        )
    console.print(f"discovered [bold]{len(leads)}[/bold] new lead(s)\n")

    if not leads and not resume:
        batch["finished_at"] = utcnow()
        return batch

    # -- N2 budgeting: the Hunter top-N is a batch-level decision ----------- #
    hunter_allowed = set(
        select_lookup_candidates(
            leads, config.hunter_top_n, enrichment_for(config)
        )
    )
    for lead in leads:
        lead["dry_run"] = dry_run or lead.get("dry_run", False)
        lead["_hunter_allowed"] = lead["lead_id"] in hunter_allowed  # type: ignore[typeddict-unknown-key]

    # -- run every lead on its own thread ---------------------------------- #
    finished: list[LeadState] = []
    with checkpointer_for(tenant_id) as saver:
        graph = build_graph(tenant_id, checkpointer=saver)

        for lead in leads:
            finished.append(run_lead(graph, tenant_id, lead, run_id))

        if resume:
            finished.extend(resume_due_threads(graph, tenant_id, run_id))

    batch["leads"] = finished
    batch["needs_manual_review"] = [
        s["lead_id"] for s in finished if s.get("needs_manual_review")
    ]
    batch["archived"] = [s["lead_id"] for s in finished if s.get("archived")]
    batch["finished_at"] = utcnow()
    return batch


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def render_batch(batch: LeadBatch) -> None:
    leads = batch.get("leads") or []
    if not leads:
        console.print("[yellow]No leads processed.[/yellow]")
        return

    table = Table(title=f"Run {batch['run_id']} - {batch['tenant_id']}")
    table.add_column("Company", overflow="fold")
    table.add_column("Region", justify="center")
    table.add_column("Fit", justify="right")
    table.add_column("Channel")
    table.add_column("Outcome")
    table.add_column("Next action", overflow="fold")

    for lead in sorted(leads, key=lambda s: -int(s.get("fit_score", 0))):
        outcome = outcome_of(lead)
        colour = {
            "sent_awaiting_reply": "green",
            "interested_manual_handoff": "bold green",
            "awaiting_manual_linkedin_send": "blue",
            "needs_manual_review": "yellow",
            "suppressed": "red",
            "blocked_compliance": "red",
        }.get(outcome, "white")
        table.add_row(
            lead.get("company_name", ""),
            lead.get("region", ""),
            str(lead.get("fit_score", "")),
            lead.get("channel", ""),
            f"[{colour}]{outcome}[/{colour}]",
            (lead.get("next_action", "") or "")[:70],
        )
    console.print(table)

    counts = summarise(leads)
    console.print("\n[bold]Outcomes:[/bold] " + "  ".join(
        f"{key}={value}" for key, value in counts.items()
    ))

    if batch.get("needs_manual_review"):
        console.print(
            f"[yellow]{len(batch['needs_manual_review'])} lead(s) need manual "
            "review.[/yellow]"
        )

    # Only leads that have never had a touch go out are genuinely queued for
    # review -- N8 resets approval_status to pending when it schedules the NEXT
    # touch, and counting those would tell the operator to review work that is
    # already sent and simply waiting on a day-gap.
    pending = [
        s for s in leads
        if s.get("approval_status") == "pending"
        and not s.get("last_touch_at")
        and not s.get("archived")
        and not s.get("needs_manual_review")
    ]
    if pending:
        console.print(
            f"\n[bold]{len(pending)} draft(s) awaiting approval.[/bold] Review them:\n"
            f"  python -m src.cli.approve --tenant {batch['tenant_id']}"
        )

    linkedin = [s for s in leads if s.get("send_status") == "pending_manual_send"]
    if linkedin and not batch.get("dry_run"):
        console.print(
            f"\n[bold]{len(linkedin)} LinkedIn message(s) queued for manual send:[/bold]\n"
            f"  python -m src.cli.linkedin_queue --tenant {batch['tenant_id']} list"
        )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one tenant's lead generation batch."
    )
    parser.add_argument("--tenant", required=True, help="tenant_id to run")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="run the full graph against the sample fixtures; never send or queue",
    )
    parser.add_argument("--limit", type=int, default=0, help="cap leads this run")
    parser.add_argument("--niche", default="", help="only this niche_id")
    parser.add_argument("--region", default="", help="only this region")
    parser.add_argument(
        "--no-resume", action="store_true",
        help="skip in-flight leads whose next touch is due; only run new discovery",
    )
    parser.add_argument(
        "--graph", action="store_true",
        help="print the compiled graph as Mermaid and exit",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.graph:
        from src.graph import render_mermaid

        print(render_mermaid(args.tenant))
        return 0

    try:
        batch = run_batch(
            args.tenant,
            dry_run=args.dry_run,
            limit=args.limit,
            resume=not args.no_resume,
            niche=args.niche,
            region=args.region,
        )
    except ConfigError as exc:
        console.print(f"[red]Halted before discovery:[/red]\n{exc}")
        return 2

    render_batch(batch)

    if args.dry_run:
        console.print(
            "\n[dim]Dry run: nothing was sent, nothing was queued, and the "
            "dedupe ledger was not written.[/dim]"
        )
    else:
        console.print(f"\n[dim]Checkpoints: {checkpoint_path(args.tenant)}[/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
