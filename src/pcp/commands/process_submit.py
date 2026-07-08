"""pcp process-submit — submit a BuildModuleWorkflow execution to a running
Temporal worker (pcp process-worker) via a running Temporal server.

Fixes a real gap, not just a thin one: activities.py/workflows.py/worker.py
existed with no client-side entry point anywhere in PCP. A worker could run
forever and nothing -- no PCP command, no code path -- could ever submit
work to it. The only way to exercise BuildModuleWorkflow was to reach past
PCP entirely and hand-craft a `temporal workflow start` invocation yourself.

Does not touch workflows.py's retry_policy (maximum_attempts=1) -- that's a
deliberate, tested, cost-justified decision (see workflows.py's own
docstring: an outer Temporal retry stacking on build.py's own internal
per-criterion retry loop cost real money in a documented incident). Nothing
here changes that.
"""

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()

TASK_QUEUE = "pcp-build"


async def _submit(target_host: str, project_root: str, module_name: str) -> dict:
    from temporalio.client import Client
    from pcp.process.workflows import BuildModuleWorkflow

    client = await Client.connect(target_host)
    return await client.execute_workflow(
        BuildModuleWorkflow.run,
        args=[project_root, module_name],
        id=f"pcp-build-{module_name}",
        task_queue=TASK_QUEUE,
    )


@click.command(name="process-submit")
@click.argument("module_name")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--target-host", default="localhost:7233",
              help="Temporal server address (default matches `temporal server start-dev`).")
def process_submit(module_name: str, project_path: str | None, target_host: str):
    """Submit one module's BuildModuleWorkflow execution.

    Requires a running `temporal server start-dev` and a running
    `pcp process-worker` first -- this command only submits the work,
    it doesn't start either of those for you.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    try:
        import temporalio  # noqa: F401
    except ImportError:
        console.print(
            "[dim]temporalio not installed — skipping. "
            "Install with: pip install program-context-protocol\\[process][/dim]"
        )
        sys.exit(0)

    console.print(f"[dim]Submitting BuildModuleWorkflow for '{module_name}' to {target_host} "
                  f"(task queue: {TASK_QUEUE})...[/dim]")
    try:
        result = asyncio.run(_submit(target_host, str(pcp_dir.parent), module_name))
    except Exception as e:
        console.print(f"[red]Workflow failed:[/red] {e}")
        sys.exit(1)

    console.print(f"[green]✓[/green] {result}")
