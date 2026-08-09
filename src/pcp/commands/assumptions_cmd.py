"""pcp assumptions — view the assumptions log; --confirm/--invalidate updates
one item's status. The log itself (.pcp/assumptions.yaml) is populated by
`pcp kickoff`/`pcp pm` (see pcp/assumptions.py's docstring); this command
never adds items, only reads and transitions their status."""

import json
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp import assumptions
from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()


@click.command("assumptions")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--confirm", "confirm_id", default=None, metavar="ITEM_ID",
              help="Mark an open assumption as confirmed (verified true).")
@click.option("--invalidate", "invalidate_id", default=None, metavar="ITEM_ID",
              help="Mark an open assumption as invalidated (turned out false). Requires --reason.")
@click.option("--reason", "reason", default=None, help="Required with --invalidate.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def assumptions_cmd(project_path: str | None, confirm_id: str | None, invalidate_id: str | None,
                     reason: str | None, as_json: bool):
    """List the assumptions log; --confirm/--invalidate transitions one item."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if confirm_id:
        found = assumptions.set_status(pcp_dir, confirm_id, "confirmed")
        console.print(
            f"[green]✓[/green] {confirm_id} marked confirmed." if found
            else f"[yellow]No open assumption found with id {confirm_id}[/yellow]"
        )
        return

    if invalidate_id:
        if not reason:
            console.print("[red]Error:[/red] --invalidate requires --reason")
            sys.exit(2)
        try:
            found = assumptions.set_status(pcp_dir, invalidate_id, "invalidated", reason)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(2)
        console.print(
            f"[yellow]⚠[/yellow] {invalidate_id} marked invalidated: {reason}" if found
            else f"[yellow]No open assumption found with id {invalidate_id}[/yellow]"
        )
        return

    items = assumptions.load(pcp_dir)
    if as_json:
        console.print(json.dumps({"assumptions": items}, indent=2, default=str))
        return

    if not items:
        console.print("[dim]No assumptions recorded yet -- populated by `pcp kickoff`/`pcp pm`.[/dim]")
        return

    table = Table(title="Assumptions Log")
    for col in ("ID", "Statement", "Status", "Source"):
        table.add_column(col)
    for i in items:
        style = "yellow" if i.get("status") == "open" else "dim"
        table.add_row(i.get("id", ""), i.get("statement", ""), i.get("status", ""), i.get("source", ""), style=style)
    console.print(table)
    console.print(
        "\n[dim]Confirm:    pcp assumptions --confirm ASxxx[/dim]\n"
        "[dim]Invalidate: pcp assumptions --invalidate ASxxx --reason \"...\"[/dim]"
    )
