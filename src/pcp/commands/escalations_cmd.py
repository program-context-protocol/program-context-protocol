"""pcp escalations — view/acknowledge the escalation ledger."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import escalations

console = Console()


@click.command("escalations")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--ack", "ack_target", default=None, metavar="MODULE/CRITERION",
              help="Acknowledge an escalation (marks a human has seen it — distinct from resolving it).")
def escalations_cmd(project_path: str | None, ack_target: str | None):
    """List escalations, staleness state, and MTTA; acknowledge with --ack."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if ack_target:
        if "/" not in ack_target:
            console.print("[red]Error:[/red] --ack expects MODULE/CRITERION (e.g. auth/A001)")
            sys.exit(2)
        module, criterion_id = ack_target.split("/", 1)
        count = escalations.acknowledge(pcp_dir, module, criterion_id)
        if count:
            console.print(f"[green]✓[/green] acknowledged {count} escalation(s) for {ack_target}")
        else:
            console.print(f"[yellow]No un-acknowledged escalations found for {ack_target}[/yellow]")
        return

    entries = escalations.load(pcp_dir)
    if not entries:
        console.print("[dim]No escalations recorded.[/dim]")
        return

    stale = {(s.get("module"), s.get("criterion_id"), s.get("timestamp")): s.get("state")
             for s in escalations.find_stale(pcp_dir)}
    table = Table(title="Escalations")
    for col in ("When", "Module/Criterion", "Category", "Acked", "State"):
        table.add_column(col)
    for e in entries:
        key = (e.get("module"), e.get("criterion_id"), e.get("timestamp"))
        state = stale.get(key, "")
        style = "red bold" if state else ""
        table.add_row(
            e.get("timestamp", ""), f"{e.get('module')}/{e.get('criterion_id')}",
            e.get("category", ""), "yes" if e.get("acknowledged_at") else "no",
            state or "ok", style=style or None,
        )
    console.print(table)
    mtta = escalations.mtta_hours(pcp_dir)
    if mtta is not None:
        console.print(f"MTTA (median time-to-acknowledge): {mtta}h")
    else:
        console.print("[dim]MTTA: no escalation has ever been acknowledged.[/dim]")
