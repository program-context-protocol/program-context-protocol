"""pcp verify-syntax-fix — dry-run, non-destructive check: does a file's
current (working-tree) content differ from its git HEAD version by nothing
but YAML quote/escape characters?

Exists specifically for the gap this can't close by itself: an external
reviewer (a human, or another system's own permission layer) that needs to
verify a claim of "this is just a syntax fix" without either trusting the
claim on its face or being able to call into PCP's own internals directly.
This command gives that reviewer an independent, deterministic, scriptable
verdict (exit 0 = safe, exit 1 = unsafe) to check *before* any write is
attempted or approved, rather than after the fact.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.commands.check import is_syntax_only_yaml_fix, _git_show_head

console = Console()


@click.command(name="verify-syntax-fix")
@click.argument("file_path", type=click.Path(exists=False))
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def verify_syntax_fix(file_path: str, project_path: str | None):
    """Verify FILE_PATH's working-tree content differs from its git HEAD
    version only by YAML quote/escape characters -- a deterministic
    SAFE/UNSAFE verdict, not a trust-based claim. Exits 0 if safe, 1 if not.

    FILE_PATH may be relative -- resolved against --path (or cwd) if so,
    not against the process's own cwd."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    raw_target = Path(file_path)
    target = (raw_target if raw_target.is_absolute() else project_root / raw_target).resolve()
    if not target.is_file():
        console.print(f"[red]Error:[/red] {file_path} does not exist")
        sys.exit(2)
    try:
        rel_path = str(target.relative_to(project_root))
    except ValueError:
        console.print(f"[red]Error:[/red] {file_path} is not inside project root {project_root}")
        sys.exit(2)

    new_text = target.read_text(errors="replace")
    old_text = _git_show_head(project_root, rel_path)

    if old_text is None:
        console.print(f"[red]UNSAFE[/red] — {rel_path}: no git HEAD version found (new file — a real addition, not a syntax fix)")
        sys.exit(1)

    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        console.print(f"[red]UNSAFE[/red] — {rel_path}: new content does not parse as valid YAML: {e}")
        sys.exit(1)

    if is_syntax_only_yaml_fix(old_text, new_text):
        console.print(f"[green]SAFE[/green] — {rel_path}: parses, and differs from HEAD only by quote/escape characters")
        sys.exit(0)

    console.print(f"[red]UNSAFE[/red] — {rel_path}: content differs beyond quoting/escaping — a real change, not a pure syntax fix")
    sys.exit(1)
