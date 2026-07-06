"""pcp ontology-status — summarize .pcp/ontology_state.yaml review counts.

Pure read, no side effects. Red items are listed by id/label explicitly so
nothing decision-critical hides behind an aggregate count.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_ontology_state, NoPCPDir

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def ontology_status(project_path: str | None):
    """Show red/blue/green counts for the current ontology draft."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    state_path = get_ontology_state(pcp_dir)
    if not state_path.exists():
        console.print("[dim]No ontology_state.yaml yet — run `pcp ontology-extract` first.[/dim]")
        sys.exit(0)

    state = yaml.safe_load(state_path.read_text()) or {}
    items = state.get("nodes", []) + state.get("edges", [])

    red = [i for i in items if i["review_status"] == "red"]
    blue = [i for i in items if i["review_status"] == "blue"]
    green = [i for i in items if i["review_status"] == "green"]

    console.print(f"Generated: {state.get('generated_at', '?')}")
    console.print(
        f"[red]{len(red)} red[/red] (low-confidence, unreviewed) — "
        f"[blue]{len(blue)} blue[/blue] (high-confidence, unreviewed) — "
        f"[green]{len(green)} green[/green] (human-verified)"
    )

    if red:
        console.print("\n[red bold]Red items — don't trust for anything decision-critical yet:[/red bold]")
        for i in red:
            label = i.get("label") or f"{i.get('source')} -{i.get('relation')}-> {i.get('target')}"
            console.print(f"  [{i['kind']}] {i['id']}: {label}")

    sys.exit(0)
