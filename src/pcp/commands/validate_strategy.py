"""pcp validate-strategy — Pass 2 pioneer claim.

Checks whether module specs collectively cover the program objective.
"""

import json
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from rich import print as rprint

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, get_objective, get_decomposition, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml
from pcp.llm import client as llm

console = Console()

SYSTEM_PROMPT = """\
You are a program-context auditor. Your job is to check whether a set of \
module specifications collectively and fully cover a stated program objective.

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "coverage_gaps": [
    {"area": "string", "quote": "string from objective"}
  ],
  "contradictions": [
    {"module": "string", "conflict": "string", "objective_quote": "string"}
  ],
  "overlaps": [
    {"modules": ["string"], "area": "string"}
  ],
  "missing_modules": [
    {"name": "string", "reason": "string"}
  ],
  "coverage_score": 0.0
}

coverage_score: 0.0 (nothing covered) to 1.0 (fully covered). Be precise.
"""


def _build_user_prompt(objective: str, decomposition: str | None, modules: dict[str, dict]) -> str:
    parts = [f"## Program Objective\n\n{objective}\n"]

    if decomposition:
        parts.append(f"## Decomposition Rationale\n\n{decomposition}\n")

    parts.append("## Module Specifications\n")
    for name, spec in modules.items():
        parts.append(f"### {name}\n```yaml\n{yaml.dump(spec, default_flow_style=False)}```\n")

    return "\n".join(parts)


def _load_modules(modules_dir: Path) -> dict[str, dict]:
    modules = {}
    if not modules_dir.exists():
        return modules
    for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
        module_name = spec_path.parent.name
        errors = validate_file(spec_path, "module_spec")
        if errors:
            console.print(f"[yellow]⚠  {spec_path.relative_to(modules_dir.parent.parent)}: schema errors[/yellow]")
            for e in errors:
                console.print(f"   {e}")
        modules[module_name] = load_yaml(spec_path)
    return modules


def _render_results(result: dict, output_json: bool) -> int:
    if output_json:
        click.echo(json.dumps(result, indent=2))
        gaps = result.get("coverage_gaps", [])
        return 1 if gaps else 0

    score = result.get("coverage_score", 0.0)
    gaps = result.get("coverage_gaps", [])
    contradictions = result.get("contradictions", [])
    overlaps = result.get("overlaps", [])
    missing = result.get("missing_modules", [])

    score_color = "green" if score >= 0.8 else "yellow" if score >= 0.5 else "red"
    console.print(f"\n[bold]Coverage score:[/bold] [{score_color}]{score:.0%}[/{score_color}]\n")

    if gaps:
        console.print("[bold red]Coverage gaps[/bold red]")
        for g in gaps:
            console.print(f"  ⚠  {g['area']}")
            if g.get("quote"):
                console.print(f"     [dim]→ \"{g['quote']}\"[/dim]")

    if contradictions:
        console.print("\n[bold red]Contradictions[/bold red]")
        for c in contradictions:
            console.print(f"  ✗  [cyan]{c['module']}[/cyan]: {c['conflict']}")

    if overlaps:
        console.print("\n[bold yellow]Overlaps[/bold yellow]")
        for o in overlaps:
            mods = ", ".join(o["modules"])
            console.print(f"  ⚠  [{mods}]: {o['area']}")

    if missing:
        console.print("\n[bold yellow]Missing modules[/bold yellow]")
        for m in missing:
            console.print(f"  ⚠  {m['name']}: {m['reason']}")

    if not gaps and not contradictions and not missing:
        console.print("[green]✓  All objective areas covered. No contradictions.[/green]")

    return 1 if gaps else 0


@click.command()
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root (default: cwd, walks up to find .pcp/).")
def validate_strategy(output_json: bool, project_path: str | None):
    """Check whether module specs cover the program objective."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    objective_path = get_objective(pcp_dir)
    if not objective_path.exists():
        console.print("[red]Error:[/red] .pcp/objective.md not found.")
        sys.exit(2)

    objective = objective_path.read_text()

    decomp_path = get_decomposition(pcp_dir)
    decomposition = decomp_path.read_text() if decomp_path.exists() else None

    modules_dir = get_modules_dir(pcp_dir)
    modules = _load_modules(modules_dir)

    if not modules:
        console.print("[yellow]No module specs found in .pcp/strategy/modules/.[/yellow]")
        console.print("Create at least one module: .pcp/strategy/modules/<name>/spec.yaml")
        sys.exit(2)

    if not output_json:
        console.print(f"[dim]Checking {len(modules)} module(s) against objective...[/dim]")

    user_prompt = _build_user_prompt(objective, decomposition, modules)

    try:
        result = llm.call_json(SYSTEM_PROMPT, user_prompt)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    exit_code = _render_results(result, output_json)
    sys.exit(exit_code)
