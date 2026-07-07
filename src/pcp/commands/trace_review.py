"""pcp trace-review — apply a human review decision to one suggested
feature-to-module link (approve -> green, reject -> removed, edit ->
relabel the module + green).
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.traceability import apply_review_action, TraceabilityError

console = Console()


@click.command()
@click.argument("link_id")
@click.option("--approve", is_flag=True, help="Confirm this feature-to-module link.")
@click.option("--reject", is_flag=True, help="Remove this suggested link.")
@click.option("--edit", "new_module", default=None,
              help="Correct the module name for this link, then mark green.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def trace_review(link_id: str, approve: bool, reject: bool, new_module: str | None,
                  project_path: str | None):
    """Review one suggested feature-to-module link by id (run
    `pcp trace-serve` or inspect traceability_map.yaml to list ids)."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    actions = [a for a in (approve, reject, bool(new_module)) if a]
    if len(actions) != 1:
        console.print("[red]Error:[/red] pass exactly one of --approve, --reject, --edit <module>.")
        sys.exit(2)

    action = "reject" if reject else "approve" if approve else "edit"

    try:
        result = apply_review_action(pcp_dir, link_id, action, new_module)
    except TraceabilityError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"[green]{result['action']}[/green] applied to link '{link_id}'.")
    sys.exit(0)
