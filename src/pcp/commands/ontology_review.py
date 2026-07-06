"""pcp ontology-review — apply a human review decision to one ontology
node/edge (approve -> green, reject -> removed, edit -> relabel + green).

Advisory, never blocks. Every decision is appended to
.pcp/ontology_review_log.jsonl with the item's confidence_score snapshotted
at review time (Goodhart defense — lets you audit later whether the
extractor's calibration was gamed or drifted after the fact).
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.ontology import apply_review_action, ReviewError

console = Console()


@click.command()
@click.argument("item_id")
@click.option("--approve", is_flag=True, help="Mark this item green (human-verified).")
@click.option("--reject", is_flag=True, help="Remove this item from the ontology.")
@click.option("--edit", "new_label", default=None,
              help="Relabel this item (node label or edge relation), then mark green.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def ontology_review(item_id: str, approve: bool, reject: bool, new_label: str | None,
                     project_path: str | None):
    """Review one ontology item by id (run `pcp ontology-status` to list ids)."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    actions = [a for a in (approve, reject, bool(new_label)) if a]
    if len(actions) != 1:
        console.print("[red]Error:[/red] pass exactly one of --approve, --reject, --edit <label>.")
        sys.exit(2)

    action = "reject" if reject else "approve" if approve else "edit"

    try:
        result = apply_review_action(pcp_dir, item_id, action, new_label)
    except ReviewError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"[green]{result['action']}[/green] applied to {result['kind']} '{item_id}'.")
    sys.exit(0)
