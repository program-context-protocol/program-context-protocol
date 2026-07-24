"""pcp narrative-lint — advisory scan of CLAUDE.md-family narrative prose
against PCP's own tracked state. Writes .pcp/narrative_lint.md. Never blocks
(see narrative_lint.py's module docstring for the fleet evidence behind this).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp import narrative_lint
from pcp import telemetry
from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()


@click.command(name="narrative-lint")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--stale-days", type=int, default=narrative_lint.STALE_DAYS_DEFAULT,
              help="Age threshold (days) for flagging a dated reference as stale.")
@click.option("--skip-llm", is_flag=True, help="Skip the semantic contradiction check (deterministic checks only).")
@click.option("--quiet", is_flag=True, help="Suppress output.")
def narrative_lint_cmd(project_path: str | None, stale_days: int, skip_llm: bool, quiet: bool):
    """Advisory lint: CLAUDE.md-family narrative prose vs. tracked state (CTRL-036). Never blocks."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    result = narrative_lint.run(pcp_dir, stale_days=stale_days, skip_llm=skip_llm)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_path = pcp_dir / "narrative_lint.md"
    out_path.write_text(narrative_lint.render_markdown(result, timestamp))

    total = len(result["stale_dates"]) + len(result["missing_files"]) + len(result["contradictions"])
    telemetry.record(
        pcp_dir, cycle="qa", check="narrative-lint", control_id="CTRL-036",
        module=None, submodule=None, criterion_id=None,
        files=result["files_scanned"], result="pass",
        errors=result["stale_dates"] + result["missing_files"] + result["contradictions"],
        error_count=total,
    )

    if quiet:
        sys.exit(0)

    color = "green" if total == 0 else "yellow"
    console.print(f"[{color}]{total} narrative-lint finding(s)[/{color}]  →  {out_path.relative_to(pcp_dir.parent)}")
    sys.exit(0)
