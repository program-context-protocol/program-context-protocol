"""pcp diff — compute and write .pcp/diff.md."""

import sys
import click
from rich.console import Console

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
def diff(project_path):
    """Compute .pcp/diff.md (target vs current state)."""
    console.print("[yellow]pcp diff not yet implemented (Phase 2).[/yellow]")
    sys.exit(0)
