"""pcp trace-map — classify which module(s) likely implement each active BRD
item, merge into .pcp/traceability_map.yaml. Advisory, never blocks.

One Haiku call per active BRD item (judge-tier, matches gate.py's routing).
Preserves prior human review decisions (green/edited/rejected) across
re-runs, same merge discipline as pcp ontology-extract.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.traceability import build_traceability, merge_with_existing, get_traceability_map
from pcp import traceability_review_log

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def trace_map(project_path: str | None):
    """Classify which module(s) implement each active BRD item, merge into
    .pcp/traceability_map.yaml, preserving prior review decisions."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    fresh = build_traceability(pcp_dir)
    if not fresh.get("available"):
        console.print(
            "[dim]No active BRD items or no modules found — nothing to classify. "
            "Run `pcp capture` to populate brd_items.yaml, or `pcp init --module` to add modules.[/dim]"
        )
        sys.exit(0)

    state_path = get_traceability_map(pcp_dir)
    existing = None
    if state_path.exists():
        existing = yaml.safe_load(state_path.read_text()) or None

    merged = merge_with_existing(fresh, existing, traceability_review_log.rejected_ids(pcp_dir))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state_path.write_text(yaml.dump(
        {"generated_at": now, "links": merged["links"]},
        default_flow_style=False, sort_keys=False,
    ))

    red = sum(1 for l in merged["links"] if l["review_status"] == "red")
    blue = sum(1 for l in merged["links"] if l["review_status"] == "blue")
    green = sum(1 for l in merged["links"] if l["review_status"] == "green")
    console.print(
        f"[green]traceability map built[/green] -> {state_path.name} "
        f"({len(merged['links'])} suggested links — "
        f"[red]{red} red[/red], [blue]{blue} blue[/blue], [green]{green} green[/green])"
    )
    sys.exit(0)
