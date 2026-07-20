"""pcp context — surface .pcp/ context to any LLM.

Not wired to fire automatically at session start on its own -- `--inject`
writes/updates a marked block in CLAUDE.md, which Claude Code already reads
every session, but nothing calls this command for you. Wire a SessionStart
hook yourself (snippet in .pcp/RECOMMENDED_PERMISSIONS.md) if you want the
block kept fresh automatically; same manual-opt-in posture as the
PreToolUse spec guard -- PCP doesn't edit .claude/settings.json itself.
"""

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console(stderr=True)

CLAUDE_MD_MARKER_START = "<!-- pcp:context:start -->"
CLAUDE_MD_MARKER_END = "<!-- pcp:context:end -->"


def _read_optional(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def _build_context(pcp_dir: Path) -> dict:
    return {
        "objective": _read_optional(pcp_dir / "objective.md"),
        "architecture": _read_optional(pcp_dir / "architecture.md"),
        "current_state": _read_optional(pcp_dir / "current_state.md"),
        "diff": _read_optional(pcp_dir / "diff.md"),
        "target_state": _read_optional(pcp_dir / "target_state.md"),
    }


def _render_markdown(ctx: dict) -> str:
    parts = ["# PCP Project Context\n"]

    if ctx["objective"]:
        parts += ["## Objective\n", ctx["objective"], ""]

    if ctx["architecture"]:
        parts += ["## Architecture\n", ctx["architecture"], ""]

    if ctx["current_state"]:
        parts += ["## Current State\n", ctx["current_state"], ""]
    else:
        parts += ["## Current State\n", "_Not generated yet. Run `pcp scan`._", ""]

    if ctx["diff"]:
        parts += ["## Pending Gaps\n", ctx["diff"], ""]

    return "\n".join(parts)


def _inject_into_claude_md(project_root: Path, markdown: str) -> None:
    claude_md = project_root / "CLAUDE.md"
    block = f"{CLAUDE_MD_MARKER_START}\n{markdown}\n{CLAUDE_MD_MARKER_END}"

    if not claude_md.exists():
        claude_md.write_text(f"{block}\n")
        console.print(f"[green]created[/green] CLAUDE.md with pcp context block")
        return

    existing = claude_md.read_text()
    start = existing.find(CLAUDE_MD_MARKER_START)
    end = existing.find(CLAUDE_MD_MARKER_END)

    if start != -1 and end != -1:
        updated = existing[:start] + block + existing[end + len(CLAUDE_MD_MARKER_END):]
        claude_md.write_text(updated)
        console.print(f"[green]updated[/green] CLAUDE.md pcp context block")
    else:
        with open(claude_md, "a") as f:
            f.write(f"\n{block}\n")
        console.print(f"[green]appended[/green] pcp context block to CLAUDE.md")


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--json", "output_json", is_flag=True,
              help="Output as JSON for IDE tool integrations.")
@click.option("--inject", is_flag=True,
              help="Write context into CLAUDE.md instead of stdout.")
def context(project_path: str | None, output_json: bool, inject: bool):
    """Output .pcp/ context for LLM consumption at session start.

    Pipe to your IDE or LLM: pcp context | pbcopy
    Inject into CLAUDE.md: pcp context --inject
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    ctx = _build_context(pcp_dir)

    if output_json:
        click.echo(json.dumps(ctx, indent=2))
        return

    markdown = _render_markdown(ctx)

    if inject:
        _inject_into_claude_md(project_root, markdown)
        return

    # Default: print to stdout (pipe-friendly)
    click.echo(markdown)
