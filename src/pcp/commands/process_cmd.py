"""pcp process-worker — start a Temporal worker for pcp build's Tier 2
process layer.

Does NOT start `temporal server start-dev` itself — that's a separate,
explicit step you run yourself, so PCP never silently launches a
long-running server process on your machine. Requires the optional
`process` extra (`pip install program-context-protocol[process]`).
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--target-host", default="localhost:7233",
              help="Temporal server address (default matches `temporal server start-dev`).")
def process_worker(project_path: str | None, target_host: str):
    """Start a Temporal worker listening on the pcp-build task queue.
    Run `temporal server start-dev` in another terminal first."""
    try:
        find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    try:
        from pcp.process.worker import main as run_worker_main
    except ImportError:
        console.print(
            "[dim]temporalio not installed — skipping. "
            "Install with: pip install program-context-protocol\\[process][/dim]"
        )
        sys.exit(0)

    console.print(f"[green]pcp process-worker[/green] connecting to {target_host} "
                  f"(task queue: pcp-build)...")
    run_worker_main(target_host)
