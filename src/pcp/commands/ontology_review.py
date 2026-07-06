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
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_ontology_state, NoPCPDir
from pcp import ontology_review_log

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

    state_path = get_ontology_state(pcp_dir)
    if not state_path.exists():
        console.print("[dim]No ontology_state.yaml — run `pcp ontology-extract` first.[/dim]")
        sys.exit(0)

    state = yaml.safe_load(state_path.read_text()) or {}
    nodes = state.get("nodes", [])
    edges = state.get("edges", [])

    item = next((n for n in nodes if n["id"] == item_id), None)
    collection, kind = nodes, "node"
    if item is None:
        item = next((e for e in edges if e["id"] == item_id), None)
        collection, kind = edges, "edge"

    if item is None:
        console.print(f"[red]Error:[/red] no node or edge with id '{item_id}' in ontology_state.yaml.")
        sys.exit(1)

    original_confidence_score = item.get("confidence_score")

    if reject:
        collection.remove(item)
        action = "reject"
    elif approve:
        item["review_status"] = "green"
        action = "approve"
    else:
        if kind == "node":
            item["label"] = new_label
        else:
            item["relation"] = new_label
        item["review_status"] = "green"
        action = "edit"

    state_path.write_text(yaml.dump(
        {"generated_at": state.get("generated_at"), "nodes": nodes, "edges": edges},
        default_flow_style=False, sort_keys=False,
    ))

    ontology_review_log.record(
        pcp_dir, item_id=item_id, kind=kind, action=action,
        original_confidence_score=original_confidence_score,
        new_label=new_label if action == "edit" else None,
    )

    console.print(f"[green]{action}[/green] applied to {kind} '{item_id}'.")
    sys.exit(0)
