"""pcp install-skill — install the full `/pcp` orchestrator skill.

Distinct from this repo's own SKILL.md (the self-install bootstrap served to
a fresh session that just needs to `pip install` and run plain CLI commands).
This installs the much larger orchestrator skill — vision workshop, parallel
build via the Workflow tool, escalation handling, multi-project status — as
a real Claude Code skill at ~/.claude/skills/pcp/SKILL.md, so `/pcp` becomes
available as a slash command.

Bundled as package data (src/pcp/skill_data/pcp/SKILL.md) rather than
downloaded, so it installs offline together with the wheel.
"""

import shutil
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

BUNDLED_SKILL_PATH = Path(__file__).parent.parent / "skill_data" / "pcp" / "SKILL.md"
DEFAULT_INSTALL_PATH = Path.home() / ".claude" / "skills" / "pcp" / "SKILL.md"


@click.command(name="install-skill")
@click.option("--force", is_flag=True, help="Overwrite an existing installed skill.")
@click.option("--path", "install_path", type=click.Path(), default=None,
              help="Override the install destination (default: ~/.claude/skills/pcp/SKILL.md).")
def install_skill(force: bool, install_path: str | None):
    """Install the /pcp orchestrator skill to ~/.claude/skills/pcp/SKILL.md."""
    if not BUNDLED_SKILL_PATH.exists():
        console.print(f"[red]Error:[/red] bundled skill not found at {BUNDLED_SKILL_PATH} "
                       "— this install may be missing package data.")
        sys.exit(2)

    dest = Path(install_path) if install_path else DEFAULT_INSTALL_PATH

    if dest.exists() and not force:
        console.print(f"[yellow]Already installed:[/yellow] {dest}")
        console.print("Use --force to overwrite with the bundled version.")
        sys.exit(1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUNDLED_SKILL_PATH, dest)
    console.print(f"[green]installed[/green] {dest}")
    console.print("[dim]/pcp is now available as a Claude Code skill.[/dim]")
