"""pcp gate — Layer 2 PR advisory gate (LLM alignment scoring)."""

import sys
import click
from rich.console import Console

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
def gate(project_path):
    """Layer 2 PR gate — LLM alignment score (advisory)."""
    console.print("[yellow]pcp gate not yet implemented (Phase 5).[/yellow]")
    sys.exit(0)
