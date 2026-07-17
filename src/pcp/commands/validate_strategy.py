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
from pcp import coupling as coupling_lib
from pcp import assertions as assertions_lib

console = Console()

# Coupling (coupling_score, coupling_violations) used to be asked of the LLM
# here — but it's graph math (circular deps, dependency counts, god modules),
# fully deterministic from each module's declared 'dependencies' field. See
# coupling.py. The LLM now only handles what's genuinely semantic: does the
# set of module specs, taken together, actually cover the stated objective.
SYSTEM_PROMPT = """\
You are a program-context auditor. Your job is to check whether a set of \
module specifications collectively and fully cover a stated program objective.

CRITICAL DISTINCTION — modules vs external systems:
The objective may name external systems, testbed targets, third-party services, \
or example integrations. These are NOT missing modules. They are systems \
OUTSIDE this codebase that will use or integrate with it. Examples: \
"test against StripeAPI", "integrate with AlphaForge", "connect to Slack" — \
these name external targets, not internal modules to build. Only report a \
missing_module when a CAPABILITY owned by this system has no module that covers it. \
Never suggest a missing module for an external system, third-party service, \
or named integration target.

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
    {"name": "string", "reason": "string", "is_internal_capability": true}
  ],
  "coverage_score": 0.0
}

coverage_score: 0.0 (nothing covered) to 1.0 (fully covered). Be precise.

missing_modules: only include capabilities this system must own but has no module for. \
  Set is_internal_capability: true only when certain. When in doubt, omit the entry.
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
        spec = load_yaml(spec_path)
        if spec.get("deprecated"):
            console.print(f"[dim]skipping deprecated module: {module_name}[/dim]")
            continue
        modules[module_name] = spec
    return modules


def _add_deterministic_coverage(result: dict, objective: str, modules: dict[str, dict]) -> dict:
    """Overrides the LLM-judged coverage_score/coverage_gaps with a
    deterministic keyword-overlap score when objective.md has a numbered
    assertion list to score against — same Goodhart-mitigation move
    coupling.py already made for coupling_score (see assertions.py's own
    docstring for the full reasoning). Falls through to the LLM's own
    coverage judgment unchanged when objective.md has no numbered list (an
    old-format file) — never hard-breaks backward compatibility. The LLM's
    own coverage_score is kept (not discarded) under llm_coverage_score so
    the two can be compared, even when the deterministic one wins."""
    assertions = assertions_lib.parse_assertions(objective)
    if not assertions:
        result["scoring_method"] = "llm"
        return result
    det = assertions_lib.compute_coverage(assertions, modules)
    result["llm_coverage_score"] = result.get("coverage_score")
    result["coverage_score"] = det["coverage_score"]
    result["coverage_gaps"] = det["coverage_gaps"]
    result["assertions_total"] = det["assertions_total"]
    result["assertions_covered"] = det["assertions_covered"]
    result["assertion_coverage_map"] = det["assertion_coverage_map"]
    result["scoring_method"] = "deterministic"
    return result


def _add_coupling(result: dict, modules: dict[str, dict]) -> dict:
    """Merge deterministic coupling analysis into the LLM's coverage-only result."""
    graph = coupling_lib.build_dependency_graph(modules)
    result.update(coupling_lib.compute_coupling(graph))
    result["communities"] = coupling_lib.compute_communities(graph)
    return result


def _add_coverage_audit(pcp_dir: Path, result: dict, objective: str, modules: dict[str, dict]) -> dict:
    """Goodhart mitigation on the LLM-judged coverage_score (see coverage_audit.py):
    never corrects the score, only surfaces internal-inconsistency and drift
    findings so a high score can't quietly substitute for a real gap-free check."""
    from pcp import coverage_audit
    findings = coverage_audit.record(
        pcp_dir, result.get("coverage_score", 0.0), result.get("coverage_gaps", []), objective, modules,
    )
    result["coverage_audit_findings"] = findings
    return result


def _coupling_color(pcp_dir: Path, coupling_score: float) -> str:
    """Prefer the human-editable Rego policy (.pcp/policies/coupling_threshold.rego)
    over the hardcoded bands below -- falls back to the hardcoded bands if opa
    isn't installed or no policy is scaffolded, so this never hard-depends on OPA."""
    from pcp import policy
    decision = policy.evaluate(pcp_dir, "data.pcp.coupling.coupling_color", {"coupling_score": coupling_score})
    if decision.get("available") and not decision.get("undefined") and decision.get("value"):
        return decision["value"]
    return "green" if coupling_score >= 0.8 else "yellow" if coupling_score >= 0.6 else "red"


def _render_results(pcp_dir: Path, result: dict, output_json: bool) -> int:
    if output_json:
        click.echo(json.dumps(result, indent=2))
        gaps = result.get("coverage_gaps", [])
        return 1 if gaps else 0

    score = result.get("coverage_score", 0.0)
    coupling_score = result.get("coupling_score", 1.0)
    gaps = result.get("coverage_gaps", [])
    contradictions = result.get("contradictions", [])
    overlaps = result.get("overlaps", [])
    missing = result.get("missing_modules", [])
    coupling_violations = result.get("coupling_violations", [])

    score_color = "green" if score >= 0.8 else "yellow" if score >= 0.5 else "red"
    coupling_color = _coupling_color(pcp_dir, coupling_score)

    method = result.get("scoring_method", "llm")
    method_label = "deterministic — keyword-overlap graph reachability" if method == "deterministic" else "LLM-judged"
    console.print(f"\n[bold]Coverage score:[/bold]  [{score_color}]{score:.0%}[/{score_color}]  [dim]({method_label})[/dim]")
    if method == "deterministic":
        console.print(f"[dim]  {result.get('assertions_covered', 0)}/{result.get('assertions_total', 0)} "
                       f"objective assertions covered — LLM's own judgment was {result.get('llm_coverage_score', 0):.0%}[/dim]")
    for finding in result.get("coverage_audit_findings", []):
        console.print(f"  [yellow]⚠  {finding}[/yellow]")
    console.print(f"[bold]Coupling score:[/bold]  [{coupling_color}]{coupling_score:.0%}[/{coupling_color}]  "
                  f"[dim](1.0 = fully decoupled, pivots are cheap)[/dim]\n")

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

    if coupling_violations:
        console.print("\n[bold red]Coupling violations[/bold red]  [dim](fix before build — each violation makes pivoting expensive)[/dim]")
        for v in coupling_violations:
            mods = " ↔ ".join(v.get("modules", []))
            vtype = v.get("type", "unknown")
            desc = v.get("description", "")
            fix = v.get("fix", "")
            type_color = "red" if vtype in ("circular", "god_module") else "yellow"
            console.print(f"  [{type_color}]{vtype}[/{type_color}]  {mods}: {desc}")
            if fix:
                console.print(f"     [dim]→ fix: {fix}[/dim]")

    if overlaps:
        console.print("\n[bold yellow]Overlaps[/bold yellow]")
        for o in overlaps:
            mods = ", ".join(o["modules"])
            console.print(f"  ⚠  [{mods}]: {o['area']}")

    if missing:
        console.print("\n[bold yellow]Missing modules[/bold yellow]")
        for m in missing:
            console.print(f"  ⚠  {m['name']}: {m['reason']}")

    severe_coupling = [v for v in coupling_violations if v.get("type") in ("circular", "god_module", "shared_state")]

    if not gaps and not contradictions and not missing and not coupling_violations:
        console.print("[green]✓  All objective areas covered. No contradictions. No coupling violations.[/green]")
    elif not gaps and not contradictions and not missing and coupling_violations:
        if severe_coupling:
            console.print("[yellow]Coverage complete — but coupling violations mean pivots will be expensive.[/yellow]")
        else:
            console.print("[dim]Coverage complete. Direct dependencies listed above are informational, not blocking.[/dim]")

    communities = result.get("communities") or {}
    if communities.get("available") and communities.get("communities"):
        console.print("\n[bold]Module clusters (graphify community detection)[/bold]  [dim]informal coupling, doesn't affect score[/dim]")
        for cid, nodes in communities["communities"].items():
            cohesion = communities.get("cohesion", {}).get(cid)
            cohesion_str = f" (cohesion {cohesion:.2f})" if cohesion is not None else ""
            console.print(f"  cluster {cid}: {', '.join(nodes)}{cohesion_str}")

    # Direct dependencies alone (no circularity, no god modules) are informational,
    # not blocking — most real projects have some. Only severe coupling and
    # coverage gaps fail the check.
    has_failures = bool(gaps or severe_coupling)
    return 1 if has_failures else 0


def run_validate_strategy(pcp_dir: Path, command: str = "validate-strategy") -> dict | None:
    """Reusable core — same check the CLI command runs, returns the result dict
    (or None if there's no objective/modules to check yet). Used by `pcp build`'s
    wave-merge gate to re-check coverage/coupling after a wave completes."""
    objective_path = get_objective(pcp_dir)
    if not objective_path.exists():
        return None
    objective = objective_path.read_text()
    decomp_path = get_decomposition(pcp_dir)
    decomposition = decomp_path.read_text() if decomp_path.exists() else None
    modules = _load_modules(get_modules_dir(pcp_dir))
    if not modules:
        return None
    user_prompt = _build_user_prompt(objective, decomposition, modules)
    result = llm.call_json(SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command=command)
    result = _add_deterministic_coverage(result, objective, modules)
    result = _add_coupling(result, modules)
    return _add_coverage_audit(pcp_dir, result, objective, modules)


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
        result = llm.call_json(SYSTEM_PROMPT, user_prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="validate-strategy")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    result = _add_deterministic_coverage(result, objective, modules)
    result = _add_coupling(result, modules)
    result = _add_coverage_audit(pcp_dir, result, objective, modules)

    exit_code = _render_results(pcp_dir, result, output_json)
    sys.exit(exit_code)
