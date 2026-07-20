"""pcp pressure-test -- MAC-Bench-style adversarial-pressure compliance
check (see pcp.pressure_test's module docstring + docs/research-
rigidity-vs-reliability-2026-07.md). Explicit, human-triggered only --
spawns TWO real coding-agent sessions, real time and cost. Never called
from `pcp build`'s own loop.
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp import pressure_test

console = Console()


@click.command(name="pressure-test")
@click.argument("module_name")
@click.argument("criterion_id")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
@click.option("--model", "build_model", default=None, help="Override the coding-agent model for both runs.")
def pressure_test_cmd(module_name: str, criterion_id: str, project_path: str | None, build_model: str | None):
    """Run a criterion twice (baseline vs. urgency/authority-pressure framing),
    each in its own throwaway worktree, and compare advisory-check violation
    counts. A widening gap under pressure is a real compliance-under-pressure
    signal -- see pcp.pressure_test's docstring."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    spec_path = pcp_dir / "strategy" / "modules" / module_name / "spec.yaml"
    acc_path = pcp_dir / "strategy" / "modules" / module_name / "acceptance.yaml"
    if not spec_path.exists() or not acc_path.exists():
        console.print(f"[red]Error:[/red] module '{module_name}' not found under .pcp/strategy/modules/")
        sys.exit(2)

    spec = load_yaml(spec_path)
    acc = load_yaml(acc_path)
    criterion = next((c for c in acc.get("criteria", []) if c["id"] == criterion_id), None)
    if criterion is None:
        console.print(f"[red]Error:[/red] criterion '{criterion_id}' not found in module '{module_name}'")
        sys.exit(2)

    mod = {"name": module_name, "spec_path": spec_path, "acc_path": acc_path, "spec": spec}

    console.print(
        f"[bold]Pressure-testing[/bold] {module_name}/{criterion_id} -- "
        "spawning 2 real coding-agent sessions (baseline + pressure framing), each in its own worktree..."
    )
    report = pressure_test.run_pressure_test(pcp_dir, project_root, mod, criterion, build_model=build_model)

    console.print(f"\n[bold]Baseline advisory findings:[/bold] {report['baseline']['total_advisory']} {report['baseline']['advisory_counts']}")
    console.print(f"[bold]Pressure advisory findings:[/bold]  {report['pressure']['total_advisory']} {report['pressure']['advisory_counts']}")
    if report["widened"]:
        console.print(
            f"[red bold]⚠ Compliance gap widened under pressure[/red bold] "
            f"(+{report['delta']} advisory findings) -- this criterion showed MORE corner-cutting "
            "when told to move fast. Worth a human look at what got skipped."
        )
    elif report["delta"] < 0:
        console.print(f"[dim]Pressure run showed FEWER advisory findings ({report['delta']}) -- no widening gap this run.[/dim]")
    else:
        console.print("[green]No compliance gap under pressure this run.[/green]")
    console.print(f"[dim]Logged to .pcp/pressure_test_log.jsonl[/dim]")
