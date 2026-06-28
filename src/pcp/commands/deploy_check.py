"""pcp deploy-check — Layer 3 deploy gate (deterministic SDLC phase exit)."""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml

console = Console()

MAX_CURRENT_STATE_AGE_HOURS = 24


def _check_criterion(criterion: dict, project_root: Path) -> tuple[bool, str]:
    check = criterion.get("check", "manual")
    target = criterion.get("target", "")

    if check == "file_exists":
        path = project_root / target
        return path.exists(), f"{'exists' if path.exists() else 'missing'}: {target}"

    elif check == "ast_pattern":
        pattern = criterion.get("pattern", "")
        if not pattern or not target:
            return criterion.get("status") == "complete", "manual (no pattern)"
        path = project_root / target
        if not path.exists():
            return False, f"file not found: {target}"
        content = path.read_text(errors="replace")
        matched = bool(re.search(pattern, content, re.MULTILINE))
        return matched, f"pattern {'found' if matched else 'not found'} in {target}"

    else:  # manual
        return criterion.get("status") == "complete", "manual"


def _check_current_state_freshness(pcp_dir: Path) -> tuple[bool, str]:
    cs = pcp_dir / "current_state.md"
    if not cs.exists():
        return False, "current_state.md not found — run `pcp scan`"
    content = cs.read_text()
    m = re.search(r"Generated: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", content)
    if not m:
        return False, "current_state.md has no timestamp"
    generated = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_hours = (now - generated).total_seconds() / 3600
    ok = age_hours <= MAX_CURRENT_STATE_AGE_HOURS
    return ok, f"current_state.md is {age_hours:.1f}h old (max {MAX_CURRENT_STATE_AGE_HOURS}h)"


@click.command()
@click.option("--phase", default=None, help="Phase name to check (default: current_phase in SDLC_phase.yaml).")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--skip-freshness", is_flag=True, help="Skip current_state.md age check.")
def deploy_check(phase: str | None, project_path: str | None, skip_freshness: bool):
    """Layer 3 deploy gate — enforce SDLC phase exit criteria."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    sdlc_path = pcp_dir / "SDLC_phase.yaml"

    if not sdlc_path.exists():
        console.print("[dim]No SDLC_phase.yaml found — skipping deploy-check.[/dim]")
        sys.exit(0)

    schema_errors = validate_file(sdlc_path, "sdlc_phase")
    if schema_errors:
        console.print("[red]SDLC_phase.yaml schema errors:[/red]")
        for e in schema_errors:
            console.print(f"  {e}")
        sys.exit(1)

    data = load_yaml(sdlc_path)
    current_phase_name = phase or data.get("current_phase")
    phases = {p["name"]: p for p in data.get("phases", [])}

    if current_phase_name not in phases:
        console.print(f"[red]Phase '{current_phase_name}' not found in SDLC_phase.yaml.[/red]")
        sys.exit(2)

    phase_data = phases[current_phase_name]
    criteria = phase_data.get("exit_criteria", [])

    console.print(f"[bold]Checking phase:[/bold] {current_phase_name}")

    failures = []
    passed = []

    if not skip_freshness:
        ok, detail = _check_current_state_freshness(pcp_dir)
        if ok:
            passed.append(f"current_state.md freshness: {detail}")
        else:
            failures.append(f"current_state.md stale: {detail}")

    for c in criteria:
        if c.get("status") == "complete":
            # Already manually marked complete — trust it
            passed.append(f"[{c['id']}] {c['description']}")
            continue
        ok, detail = _check_criterion(c, project_root)
        if ok:
            passed.append(f"[{c['id']}] {c['description']}")
        else:
            failures.append(f"[{c['id']}] {c['description']} → {detail}")

    for p in passed:
        console.print(f"  [green]✓[/green]  {p}")

    if failures:
        console.print(f"\n[red bold]BLOCKED — {len(failures)} exit criteria not met:[/red bold]")
        for f in failures:
            console.print(f"  [red]✗[/red]  {f}")
        sys.exit(1)

    console.print(f"\n[green bold]✓  Phase '{current_phase_name}' exit criteria met.[/green bold]")
    sys.exit(0)
