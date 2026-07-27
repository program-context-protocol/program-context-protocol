"""pcp objective-conflicts — view/dismiss flagged objective-vs-business-decision
conflicts (CTRL-035). Unresolved ones hard-block `pcp build`."""

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import objective_conflicts

console = Console()


@click.command("objective-conflicts")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--dismiss", "dismiss_id", default=None, metavar="ITEM_ID",
              help="Dismiss a flagged conflict without editing objective.md (false positive). Requires --reason.")
@click.option("--reason", "reason", default=None, help="Required with --dismiss.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def objective_conflicts_cmd(project_path: str | None, dismiss_id: str | None, reason: str | None, as_json: bool):
    """List objective-conflict flags; --dismiss clears a false positive."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if dismiss_id:
        if not reason:
            console.print("[red]Error:[/red] --dismiss requires --reason")
            sys.exit(2)
        found = objective_conflicts.dismiss(pcp_dir, dismiss_id, reason)
        if found:
            console.print(f"[green]✓[/green] dismissed {dismiss_id}: {reason}")
        else:
            console.print(f"[yellow]No unresolved conflict found with id {dismiss_id}[/yellow]")
        return

    unresolved = objective_conflicts.reconcile(pcp_dir)

    if as_json:
        import json as _json
        console.print(_json.dumps({"unresolved": unresolved}, indent=2))
        return

    if not unresolved:
        console.print("[green]No unresolved objective conflicts.[/green] `pcp build` is not blocked by this gate.")
        return

    table = Table(title="Unresolved Objective Conflicts (blocking pcp build)")
    for col in ("ID", "Description", "Conflict", "Source"):
        table.add_column(col)
    for c in unresolved:
        table.add_row(
            c.get("id", ""), c.get("description", ""), c.get("drift_flag", ""), c.get("source", ""),
        )
    console.print(table)
    # Not "edit by hand": these files are human-AUTHORIZED, not human-typed,
    # and `correct-objective --from-conflict` exists to pull the correction
    # text straight out of the flagged item and diff it for approval.
    first_id = unresolved[0].get("id", "<ID>")
    console.print(
        f"\n[dim]Resolve:         pcp correct-objective --from-conflict {first_id}[/dim]\n"
        f"[dim]False positive:  pcp objective-conflicts --dismiss {first_id} --reason \"...\"[/dim]"
    )
