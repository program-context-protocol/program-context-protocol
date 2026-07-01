"""pcp diff — compute .pcp/diff.md (target_state vs current_state)."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir

console = Console()


def _extract_pending(current_state_path: Path) -> list[str]:
    """Pull pending criteria lines from current_state.md."""
    if not current_state_path.exists():
        return []
    pending = []
    for line in current_state_path.read_text().splitlines():
        if re.match(r"- \[ \]", line.strip()):
            pending.append(line.strip()[6:])  # strip "- [ ] "
    return pending


def _extract_coverage_score(current_state_path: Path) -> str:
    if not current_state_path.exists():
        return "unknown"
    m = re.search(r"acceptance coverage: ([\d.]+)", current_state_path.read_text())
    return f"{float(m.group(1)):.0%}" if m else "unknown"


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=None)
def diff(project_path: str | None):
    """Compute .pcp/diff.md — target state vs current state gap."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    target_state_path = pcp_dir / "target_state.md"
    current_state_path = pcp_dir / "current_state.md"

    if not target_state_path.exists():
        console.print("[yellow]No target_state.md found. Create it to track the ideal end state.[/yellow]")
        sys.exit(0)

    if not current_state_path.exists():
        console.print("[yellow]No current_state.md found. Run `pcp scan` first.[/yellow]")
        sys.exit(2)

    pending = _extract_pending(current_state_path)
    score = _extract_coverage_score(current_state_path)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        "# Diff — Target vs Current State",
        f"Computed: {timestamp}",
        f"Coverage: {score}",
        "",
        "## Target State",
        "",
        target_state_path.read_text().strip(),
        "",
        "## Pending Gaps",
        "",
    ]

    if pending:
        for p in pending:
            lines.append(f"- [ ] {p}")
    else:
        lines.append("_No pending criteria — all acceptance criteria met._")

    lines += ["", "## Next Actions", ""]
    if pending:
        lines.append("Implement the pending criteria above. Re-run `pcp scan` after each change.")
    else:
        lines.append("All acceptance criteria met. Advance SDLC phase via `pcp deploy-check`.")

    diff_md = pcp_dir / "diff.md"
    diff_md.write_text("\n".join(lines) + "\n")

    console.print(
        f"[dim]{len(pending)} pending gap(s)[/dim]  →  {diff_md.relative_to(project_root)}"
    )
