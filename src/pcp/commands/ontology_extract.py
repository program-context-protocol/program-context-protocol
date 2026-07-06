"""pcp ontology-extract — run graphify extraction, merge into .pcp/ontology_state.yaml.

Advisory, never blocks. Requires the optional `graph` extra
(`pip install program-context-protocol[graph]`) — degrades gracefully with a
one-line message if graphify isn't installed, same posture as every other
optional-tool wrapper in this codebase.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_ontology_state, NoPCPDir
from pcp.ontology import extract_ontology, merge_with_existing
from pcp import ontology_review_log

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def ontology_extract(project_path: str | None):
    """Extract an ontology draft (nodes/edges) via graphify and merge into
    .pcp/ontology_state.yaml, preserving any prior human review decisions."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    fresh = extract_ontology(project_root)

    if not fresh.get("available"):
        console.print(
            "[dim]graphify not installed — skipping ontology extraction. "
            "Install with: pip install program-context-protocol\\[graph][/dim]"
        )
        sys.exit(0)

    state_path = get_ontology_state(pcp_dir)
    existing = None
    if state_path.exists():
        existing = yaml.safe_load(state_path.read_text()) or None

    merged = merge_with_existing(fresh, existing, ontology_review_log.rejected_ids(pcp_dir))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(yaml.dump(
        {"generated_at": now, "nodes": merged["nodes"], "edges": merged["edges"]},
        default_flow_style=False, sort_keys=False,
    ))

    red = sum(1 for n in merged["nodes"] + merged["edges"] if n["review_status"] == "red")
    blue = sum(1 for n in merged["nodes"] + merged["edges"] if n["review_status"] == "blue")
    green = sum(1 for n in merged["nodes"] + merged["edges"] if n["review_status"] == "green")
    console.print(
        f"[green]ontology extracted[/green] -> {state_path.name} "
        f"({len(merged['nodes'])} nodes, {len(merged['edges'])} edges — "
        f"[red]{red} red[/red], [blue]{blue} blue[/blue], [green]{green} green[/green])"
    )
    sys.exit(0)
