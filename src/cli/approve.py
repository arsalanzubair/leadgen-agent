"""
approve.py -- human approval review CLI (N5).

    python -m src.cli.approve --tenant example_tenant
    python -m src.cli.approve --tenant example_tenant --channel linkedin
    python -m src.cli.approve --tenant example_tenant --list

Lists every lead whose graph thread is interrupted awaiting approval, GROUPED
BY CHANNEL, showing company, channel, fit_reason and the draft. Accepts:

    a  approve as drafted
    e  edit  -- opens a prompt for replacement text, field by field
    r  reject
    s  skip  -- leave it in the queue for later
    q  quit  -- stop reviewing, everything unreviewed stays queued

Backed by the per-tenant SqliteSaver, so the queue survives restarts: quitting
half way through and coming back on Thursday resumes exactly where you were.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Iterable

from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.graph import build_graph, checkpointer_for, thread_config
from src.nodes.n0_config_load import load_tenant_config
from src.settings import checkpoint_path

console = Console()


# --------------------------------------------------------------------------- #
# Finding interrupted threads
# --------------------------------------------------------------------------- #

def pending_threads(tenant_id: str) -> list[dict[str, Any]]:
    """
    Every checkpointed thread for this tenant that is paused on an interrupt.

    Reads the checkpointer directly rather than keeping a side table: the
    checkpoint IS the queue, so the two can never disagree about what is
    actually awaiting a decision.
    """
    items: list[dict[str, Any]] = []
    with checkpointer_for(tenant_id) as saver:
        graph = build_graph(tenant_id, checkpointer=saver)

        # Materialise the thread ids BEFORE querying any of them. `saver.list()`
        # is a generator over a live SQLite cursor, and `get_state()` issues its
        # own query on the same connection -- interleaving them deadlocks the
        # CLI on the database lock rather than failing, which looks like a hang.
        thread_ids = list(dict.fromkeys(
            checkpoint.config.get("configurable", {}).get("thread_id", "")
            for checkpoint in saver.list(None)
        ))

        for thread_id in thread_ids:
            if not thread_id:
                continue
            snapshot = graph.get_state(thread_config(tenant_id, thread_id))
            if not snapshot.interrupts:
                continue
            payload = snapshot.interrupts[0].value
            if not isinstance(payload, dict):
                continue
            items.append({"thread_id": thread_id, "payload": payload, "state": snapshot.values})
    items.sort(key=lambda item: (
        item["payload"].get("channel", ""),
        -int(item["payload"].get("fit_score", 0)),
    ))
    return items


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_summary(items: list[dict[str, Any]]) -> None:
    table = Table(title="Pending approvals", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Channel")
    table.add_column("Company")
    table.add_column("Region", justify="center")
    table.add_column("Fit", justify="right")
    table.add_column("Touch", justify="right")
    table.add_column("Signal referenced", overflow="fold")
    for index, item in enumerate(items, start=1):
        payload = item["payload"]
        table.add_row(
            str(index),
            payload.get("channel", ""),
            payload.get("company_name", ""),
            payload.get("region", ""),
            str(payload.get("fit_score", "")),
            str(payload.get("sequence_step", 1)),
            (payload.get("signal_referenced", "") or "-")[:60],
        )
    console.print(table)


def render_lead(payload: dict[str, Any], position: str) -> None:
    header = (
        f"[bold]{payload.get('company_name', '')}[/bold]  "
        f"[dim]{payload.get('region', '')} / {payload.get('niche_id', '')}[/dim]"
    )
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", justify="right")
    meta.add_column()
    meta.add_row("channel", payload.get("channel", ""))
    meta.add_row("touch", str(payload.get("sequence_step", 1)))
    meta.add_row("fit", f"{payload.get('fit_score', '')} - {payload.get('fit_reason', '')}")
    if payload.get("contact_name") or payload.get("contact_email"):
        meta.add_row(
            "contact",
            f"{payload.get('contact_name', '')} <{payload.get('contact_email', '')}>".strip(),
        )
    if payload.get("linkedin_url"):
        meta.add_row("linkedin", payload["linkedin_url"])
    meta.add_row("language", payload.get("language", "") + (
        " [yellow](translated)[/yellow]" if payload.get("translated") else ""
    ))
    meta.add_row("signal used", payload.get("signal_referenced", "") or "[red]none[/red]")

    console.print(Panel(meta, title=header, subtitle=position, border_style="cyan"))

    if payload.get("signals"):
        console.print("[dim]All signals found:[/dim]")
        for signal in payload["signals"]:
            console.print(f"  [dim]-[/dim] {signal}")
        console.print()

    email = payload.get("email") or {}
    if email:
        console.print(Panel(
            f"[bold]Subject:[/bold] {email.get('subject', '')}\n\n{email.get('body', '')}",
            title="EMAIL", border_style="green",
        ))

    linkedin = payload.get("linkedin") or {}
    if linkedin:
        note = linkedin.get("connection_note", "")
        colour = "green" if len(note) <= 300 else "red"
        body = f"[bold]Connection note[/bold] ([{colour}]{len(note)}/300 chars[/{colour}]):\n{note}"
        if linkedin.get("followup_dm"):
            body += f"\n\n[bold]Follow-up DM:[/bold]\n{linkedin['followup_dm']}"
        console.print(Panel(body, title="LINKEDIN (manual send)", border_style="blue"))


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

def _prompt_multiline(label: str, current: str) -> str:
    console.print(f"\n[bold]{label}[/bold] [dim](blank line to finish, "
                  f"'.' alone to keep the current text)[/dim]")
    console.print(f"[dim]current:[/dim] {current[:200]}")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == ".":
            return current
        if not line and lines:
            break
        if not line and not lines:
            return current
        lines.append(line)
    return "\n".join(lines).strip() or current


def collect_edits(payload: dict[str, Any]) -> dict[str, Any]:
    decision: dict[str, Any] = {"action": "edit"}
    email = payload.get("email") or {}
    if email:
        decision["email"] = {
            "subject": _prompt_multiline("Subject", email.get("subject", "")),
            "body": _prompt_multiline("Body", email.get("body", "")),
        }
    linkedin = payload.get("linkedin") or {}
    if linkedin:
        note = _prompt_multiline("Connection note (max 300 chars)",
                                 linkedin.get("connection_note", ""))
        while len(note) > 300:
            console.print(f"[red]That is {len(note)} characters; the limit is 300.[/red]")
            note = _prompt_multiline("Connection note (max 300 chars)", note)
        decision["linkedin"] = {
            "connection_note": note,
            "followup_dm": _prompt_multiline("Follow-up DM", linkedin.get("followup_dm", "")),
        }
    return decision


# --------------------------------------------------------------------------- #
# The review loop
# --------------------------------------------------------------------------- #

def review(
    tenant_id: str,
    *,
    channel_filter: str = "",
    items: list[dict[str, Any]] | None = None,
    reader=input,
) -> dict[str, int]:
    """
    Walk the queue. `reader` is injectable so the loop is testable without a
    terminal.
    """
    items = pending_threads(tenant_id) if items is None else items
    if channel_filter:
        items = [i for i in items if i["payload"].get("channel") == channel_filter]

    if not items:
        console.print("[green]Nothing awaiting approval.[/green]")
        return {"approved": 0, "edited": 0, "rejected": 0, "skipped": 0}

    render_summary(items)
    tally = {"approved": 0, "edited": 0, "rejected": 0, "skipped": 0}

    with checkpointer_for(tenant_id) as saver:
        graph = build_graph(tenant_id, checkpointer=saver)
        current_channel = None

        for index, item in enumerate(items, start=1):
            payload = item["payload"]
            channel = payload.get("channel", "")
            if channel != current_channel:
                current_channel = channel
                console.rule(f"[bold]{channel.upper()}[/bold]")

            render_lead(payload, f"{index}/{len(items)}")

            while True:
                console.print(
                    "\n[bold](a)[/bold]pprove  [bold](e)[/bold]dit  "
                    "[bold](r)[/bold]eject  [bold](s)[/bold]kip  [bold](q)[/bold]uit"
                )
                try:
                    choice = (reader("> ") or "").strip().lower()[:1]
                except (EOFError, KeyboardInterrupt):
                    console.print("\n[yellow]Stopped. Unreviewed leads stay queued.[/yellow]")
                    return tally

                if choice == "q":
                    console.print("[yellow]Stopped. Unreviewed leads stay queued.[/yellow]")
                    return tally
                if choice == "s":
                    tally["skipped"] += 1
                    break
                if choice in ("a", "e", "r"):
                    decision: Any = {"action": {"a": "approve", "e": "edit", "r": "reject"}[choice]}
                    if choice == "e":
                        decision = collect_edits(payload)
                    graph.invoke(
                        Command(resume=decision),
                        config=thread_config(tenant_id, item["thread_id"]),
                    )
                    key = {"a": "approved", "e": "edited", "r": "rejected"}[choice]
                    tally[key] += 1
                    console.print(f"[green]{key}.[/green]")
                    break
                console.print("[red]Unrecognised. Enter a, e, r, s or q.[/red]")

    return tally


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and approve outreach drafts (N5).")
    parser.add_argument("--tenant", required=True, help="tenant_id to review")
    parser.add_argument(
        "--channel", default="", choices=["", "email", "linkedin", "both"],
        help="only review one channel's queue",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="print the queue and exit without reviewing",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        load_tenant_config(args.tenant)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]{exc}[/red]")
        return 2

    db = checkpoint_path(args.tenant)
    if not db.exists():
        console.print(
            f"[yellow]No checkpoint database for tenant '{args.tenant}' at {db}.[/yellow]\n"
            "Run a batch first: python -m src.cli.run_batch --tenant "
            f"{args.tenant} --dry-run"
        )
        return 0

    items = pending_threads(args.tenant)
    if args.channel:
        items = [i for i in items if i["payload"].get("channel") == args.channel]

    if args.list_only:
        if items:
            render_summary(items)
        else:
            console.print("[green]Nothing awaiting approval.[/green]")
        return 0

    tally = review(args.tenant, channel_filter=args.channel, items=items)
    console.print(
        f"\n[bold]Done.[/bold] approved={tally['approved']} edited={tally['edited']} "
        f"rejected={tally['rejected']} skipped={tally['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
