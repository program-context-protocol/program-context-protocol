"""pcp report — DEPRECATED (2026-07-21).

Confirmed a genuine duplicate, not a distinct capability: this command had
its own separate implementation reading bypass_log.yaml/current_state.md
directly, never calling into provenance.py's build_provenance(). Every
data point it showed already exists elsewhere -- the bypass ledger is a
full section of `pcp provenance`'s output; the coverage percentage is
already in current_state.md itself (which this command only re-read) and
surfaced again in pcp.md/dashboard.html. Zero unique data, duplicate code
instead of reuse -- exactly the catalog-bloat pattern PCP's own
self-evaluation named. Kept as a command (not removed outright) so a
script or muscle-memory invocation gets a clear redirect instead of a
"command not found" error.
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

_DEPRECATION_MESSAGE = (
    "`pcp report` is deprecated -- use `pcp provenance` (bypass ledger + SSDF crosswalk) "
    "or `pcp dashboard` (Audit Trail tab) instead. Both already show everything this "
    "command did, with more context."
)


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON.")
def report(project_path: str | None, output_json: bool):
    """DEPRECATED -- use `pcp provenance` or `pcp dashboard` instead."""
    try:
        find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if output_json:
        click.echo(json.dumps({"deprecated": True, "use_instead": ["pcp provenance", "pcp dashboard"]}, indent=2))
        return

    console.print(f"[yellow]{_DEPRECATION_MESSAGE}[/yellow]")
