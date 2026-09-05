"""
linkedin_queue.py -- pending manual LinkedIn sends (N6b).

    python -m src.cli.linkedin_queue --tenant example_tenant list
    python -m src.cli.linkedin_queue --tenant example_tenant show <lead_id>
    python -m src.cli.linkedin_queue --tenant example_tenant sent <lead_id>
    python -m src.cli.linkedin_queue --tenant example_tenant connected <lead_id>
    python -m src.cli.linkedin_queue --tenant example_tenant skip <lead_id>

Lists approved, compliance-cleared messages waiting to be sent BY HAND through
the LinkedIn interface, and marks one actioned once the human has actually done
it. Nothing in this file automates LinkedIn, and nothing in it should.

`connected` is the important one operationally: marking a lead connected is
what unblocks cadence step 2 (the post-acceptance DM) in N8. Until then the
sequence waits, because DMing someone who never accepted is not possible.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n6b_linkedin_outreach import all_items, mark, pending_items
from src.reliability import ConfigError

console = Console()

STATUS_STYLE = {
    "pending": "yellow",
    "sent": "green",
    "connected": "bold green",
    "skipped": "dim",
}


def render_list(items: list[dict], title: str) -> None:
    if not items:
        console.print(f"[green]{title}: nothing here.[/green]")
        return

    table = Table(title=title, show_lines=False)
    table.add_column("Status")
    table.add_column("Company")
    table.add_column("Contact")
    table.add_column("Region", justify="center")
    table.add_column("Fit", justify="right")
    table.add_column("Touch", justify="right")
    table.add_column("lead_id", style="dim", overflow="fold")
    for item in items:
        status = item.get("status", "pending")
        table.add_row(
            f"[{STATUS_STYLE.get(status, 'white')}]{status}[/]",
            item.get("company_name", ""),
            item.get("contact_name", "") or "-",
            item.get("region", ""),
            str(item.get("fit_score", "")),
            str(item.get("sequence_step", 1)),
            item.get("lead_id", ""),
        )
    console.print(table)


def render_item(item: dict) -> None:
    """The full text, formatted to be copied straight into LinkedIn."""
    note = item.get("connection_note", "")
    header = (
        f"[bold]{item.get('company_name', '')}[/bold] - "
        f"{item.get('contact_name', '') or 'contact unknown'}"
    )
    console.print(Panel(
        f"[dim]send from:[/dim] {item.get('account_label', '')}\n"
        f"[dim]profile:  [/dim] {item.get('linkedin_url', '')}\n"
        f"[dim]touch:    [/dim] {item.get('sequence_step', 1)}\n"
        f"[dim]fit:      [/dim] {item.get('fit_score', '')} - {item.get('fit_reason', '')}",
        title=header, border_style="blue",
    ))
    if note:
        colour = "green" if len(note) <= 300 else "red"
        console.print(Panel(
            note, title=f"CONNECTION NOTE ([{colour}]{len(note)}/300[/{colour}])",
            border_style="cyan",
        ))
    if item.get("followup_dm"):
        console.print(Panel(
            item["followup_dm"],
            title="FOLLOW-UP DM (send only after they accept)", border_style="magenta",
        ))
    console.print(
        "[dim]Send it in LinkedIn yourself, then run:[/dim] "
        f"python -m src.cli.linkedin_queue --tenant {item.get('tenant_id', '<tenant>')} "
        f"sent {item.get('lead_id', '')}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Work the manual LinkedIn send queue (N6b). "
                    "This tool never touches LinkedIn itself.",
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument(
        "command",
        choices=["list", "all", "show", "sent", "connected", "skip"],
        help="list: pending only | all: every item | show/sent/connected/skip: "
             "act on one lead_id",
    )
    parser.add_argument("lead_id", nargs="?", default="")
    parser.add_argument(
        "--step", type=int, default=None,
        help="target a specific cadence touch instead of the oldest pending one",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        load_tenant_config(args.tenant)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    if args.command == "list":
        items = pending_items(args.tenant)
        render_list(items, f"Pending manual LinkedIn sends - {args.tenant}")
        if items:
            console.print(
                f"\n[dim]{len(items)} to send by hand. "
                f"`show <lead_id>` prints one ready to copy.[/dim]"
            )
        return 0

    if args.command == "all":
        render_list(all_items(args.tenant), f"LinkedIn queue - {args.tenant}")
        return 0

    if not args.lead_id:
        console.print(f"[red]`{args.command}` needs a lead_id.[/red]")
        return 2

    if args.command == "show":
        matches = [i for i in all_items(args.tenant) if i.get("lead_id") == args.lead_id]
        if not matches:
            console.print(f"[red]No queued item for lead_id {args.lead_id}.[/red]")
            return 1
        for item in matches:
            item.setdefault("tenant_id", args.tenant)
            render_item(item)
        return 0

    status = {"sent": "sent", "connected": "connected", "skip": "skipped"}[args.command]
    item = mark(args.tenant, args.lead_id, status, sequence_step=args.step)
    if item is None:
        console.print(f"[red]No queued item for lead_id {args.lead_id}.[/red]")
        return 1

    console.print(
        f"[green]{item.get('company_name', args.lead_id)} marked {status}.[/green]"
    )
    if status == "connected":
        console.print(
            "[dim]The post-acceptance DM becomes due at the next scheduled run.[/dim]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
