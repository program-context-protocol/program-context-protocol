"""pcp takeover — one-shot: preflight -> kickoff -> autonomous build.

The single entrypoint for "point pcp at a vision doc and let it run the
project." Chains the existing doctor/kickoff/build commands rather than
duplicating their logic.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.commands.build import build
from pcp.commands.doctor import check_environment
from pcp.commands.kickoff import kickoff

console = Console()


def _current_phase(pcp_dir: Path) -> str | None:
    path = pcp_dir / "SDLC_phase.yaml"
    if not path.exists():
        return None
    return (yaml.safe_load(path.read_text()) or {}).get("current_phase")


@click.command()
@click.argument("vision_file", type=click.Path(exists=True))
@click.option("--path", "project_path", type=click.Path(), default=".",
              help="Project root (default: current directory).")
@click.option("--force", is_flag=True, help="Force overwrite existing .pcp/ directory.")
def takeover(vision_file: str, project_path: str, force: bool):
    """Take over a project end-to-end: preflight, kickoff from a vision doc, then build every pending criterion."""
    root = Path(project_path).resolve()
    pcp_dir = root / ".pcp"
    ctx = click.get_current_context()

    console.print("[bold]Step 1/3 — environment preflight[/bold]")
    check_environment(pcp_dir, fatal_on_missing_required=True)

    console.print("\n[bold]Step 2/3 — kickoff from vision[/bold]")
    ctx.invoke(kickoff, vision_file=vision_file, project_path=project_path, force=force)

    phase = _current_phase(pcp_dir)
    if phase in (None, "planning"):
        console.print(
            "\n[yellow]Strategy not approved — stopping before build. "
            "Re-run `pcp takeover` once you're ready.[/yellow]"
        )
        sys.exit(0)

    console.print("\n[bold]Step 3/3 — autonomous build[/bold]")
    ctx.invoke(build, module_name=None, project_path=project_path)
