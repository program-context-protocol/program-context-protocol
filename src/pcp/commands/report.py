"""pcp report — surface bypass history and coverage trends."""

import json
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()


def _load_bypass_log(pcp_dir: Path) -> list[dict]:
    log_path = pcp_dir / "bypass_log.yaml"
    if not log_path.exists():
        return []
    data = yaml.safe_load(log_path.read_text()) or {}
    return data.get("bypasses", [])


def _load_coverage(pcp_dir: Path) -> str:
    cs = pcp_dir / "current_state.md"
    if not cs.exists():
        return "unknown (run pcp scan)"
    for line in cs.read_text().splitlines():
        if "acceptance coverage:" in line:
            return line.split(":", 1)[1].strip()
    return "unknown"


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON.")
def report(project_path: str | None, output_json: bool):
    """Show bypass history, coverage score, and gate outcomes."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    bypasses = _load_bypass_log(pcp_dir)
    coverage = _load_coverage(pcp_dir)

    if output_json:
        click.echo(json.dumps({
            "coverage_score": coverage,
            "bypass_count": len(bypasses),
            "bypasses": bypasses,
        }, indent=2))
        return

    console.print(f"\n[bold]PCP Report[/bold]  [dim]{pcp_dir.parent}[/dim]\n")
    console.print(f"  Acceptance coverage : [bold]{coverage}[/bold]")
    console.print(f"  Bypass count        : [bold]{len(bypasses)}[/bold]\n")

    if not bypasses:
        console.print("[dim]No bypasses logged.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Timestamp", style="dim")
    table.add_column("Reason")
    table.add_column("Rules bypassed")

    for b in bypasses:
        rules = ", ".join(b.get("rules_bypassed", []))
        table.add_row(b.get("timestamp", ""), b.get("reason", ""), rules)

    console.print(table)
