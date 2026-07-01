"""pcp validate-module <name> — per-module spec alignment check."""

import json
import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, get_objective, get_decomposition, NoPCPDir
from pcp.schema.validator import validate_file, load_yaml
from pcp.llm import client as llm

console = Console()

SYSTEM_PROMPT = """\
You are a program-context auditor. Check whether a single module specification \
is aligned with the program objective and decomposition rationale.

Output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "alignment_score": 0.0,
  "aligned": true,
  "gaps": ["string"],
  "contradictions": ["string"],
  "decomposition_conflicts": ["string"],
  "suggestions": ["string"]
}

alignment_score: 0.0 (no alignment) to 1.0 (perfectly aligned).
aligned: true if alignment_score >= 0.7 and no contradictions.
"""


def _build_prompt(objective: str, decomposition: str | None, module_name: str, spec: dict) -> str:
    parts = [f"## Program Objective\n\n{objective}\n"]
    if decomposition:
        parts.append(f"## Decomposition Rationale\n\n{decomposition}\n")
    parts.append(f"## Module to Validate: {module_name}\n")
    parts.append(f"```yaml\n{yaml.dump(spec, default_flow_style=False)}```\n")
    parts.append(
        f"Does the '{module_name}' module spec align with the objective and decomposition? "
        "Are its objective_coverage claims accurate? Does it conflict with the decomposition?"
    )
    return "\n".join(parts)


@click.command()
@click.argument("module_name")
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON.")
@click.option("--path", "project_path", type=click.Path(), default=None)
def validate_module(module_name: str, output_json: bool, project_path: str | None):
    """Check whether a module spec aligns with the objective and decomposition."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    modules_dir = get_modules_dir(pcp_dir)
    spec_path = modules_dir / module_name / "spec.yaml"

    if not spec_path.exists():
        console.print(f"[red]Module '{module_name}' not found:[/red] {spec_path}")
        console.print(f"Available: {', '.join(p.parent.name for p in modules_dir.glob('*/spec.yaml'))}")
        sys.exit(2)

    errors = validate_file(spec_path, "module_spec")
    if errors:
        console.print(f"[yellow]⚠  schema errors in {module_name}/spec.yaml:[/yellow]")
        for e in errors:
            console.print(f"   {e}")

    spec = load_yaml(spec_path)
    if spec.get("deprecated"):
        console.print(f"[dim]{module_name} is deprecated — skipping.[/dim]")
        sys.exit(0)

    objective_path = get_objective(pcp_dir)
    if not objective_path.exists():
        console.print("[red]Error:[/red] .pcp/objective.md not found.")
        sys.exit(2)

    objective = objective_path.read_text()
    decomp_path = get_decomposition(pcp_dir)
    decomposition = decomp_path.read_text() if decomp_path.exists() else None

    if not output_json:
        console.print(f"[dim]Validating module '{module_name}'...[/dim]")

    try:
        result = llm.call_json(
            SYSTEM_PROMPT, _build_prompt(objective, decomposition, module_name, spec),
            model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="validate-module",
        )
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    if output_json:
        click.echo(json.dumps(result, indent=2))
        sys.exit(0 if result.get("aligned") else 1)

    score = result.get("alignment_score", 0.0)
    aligned = result.get("aligned", False)
    color = "green" if aligned else "red"

    console.print(f"\n[bold]Module:[/bold] {module_name}")
    console.print(f"[bold]Alignment:[/bold] [{color}]{score:.0%}[/{color}]  {'✓' if aligned else '✗'}\n")

    for gap in result.get("gaps", []):
        console.print(f"  [yellow]⚠  Gap:[/yellow] {gap}")
    for c in result.get("contradictions", []):
        console.print(f"  [red]✗  Contradiction:[/red] {c}")
    for dc in result.get("decomposition_conflicts", []):
        console.print(f"  [red]✗  Decomp conflict:[/red] {dc}")
    for s in result.get("suggestions", []):
        console.print(f"  [dim]→  {s}[/dim]")

    if aligned and not result.get("gaps") and not result.get("contradictions"):
        console.print("[green]✓  Module spec is aligned.[/green]")

    sys.exit(0 if aligned else 1)
