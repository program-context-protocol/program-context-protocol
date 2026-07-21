"""pcp control-audit -- self-evaluation gap closed 2026-07-21. See
control_audit.py's module docstring for the full rationale."""

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import control_audit

console = Console()


@click.command(name="control-audit")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON.")
def control_audit_cmd(project_path: str | None, output_json: bool):
    """Which cataloged controls have never fired -- retire/merge review candidates."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    audit = control_audit.write_control_audit(pcp_dir)

    if output_json:
        click.echo(json.dumps(audit, indent=2))
        return

    never_fired = {k: v for k, v in audit.items() if v["signal"] == "never-fired"}
    console.print(f"[bold]Control Catalog Audit[/bold] — {len(audit)} controls, {len(never_fired)} never-fired")
    for cid, v in sorted(never_fired.items()):
        console.print(f"  [yellow]{cid}[/yellow] {v['name']} — {v['total_runs']} runs, 0 findings")
    console.print("[dim]Full report: .pcp/control_audit.md[/dim]")
