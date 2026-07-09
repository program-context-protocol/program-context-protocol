"""pcp pm — translate natural language intent to spec modifications."""

import os
import sys
import json
from pathlib import Path
import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir, get_modules_dir
from pcp.llm import client as llm
from pcp.pcp_status import write_pcp_md
from pcp.commands.kickoff import _normalize_acceptance, _normalize_spec

console = Console()

def _max_context_chars() -> int:
    """Reject-loud, not truncate-silent -- same posture as kickoff.py's
    vision-doc guard. Bounds _load_project_context's assembled prompt (every
    existing module's full spec.yaml + acceptance.yaml, pasted verbatim,
    unbounded before this). A function, not a module-level constant, so
    PCP_PM_MAX_CONTEXT_CHARS is read live at call time rather than frozen at
    import time."""
    return int(os.environ.get("PCP_PM_MAX_CONTEXT_CHARS", "60000"))

SYSTEM_PROMPT = """\
You are an expert product manager.
Your task is to take a feature intent expressed in natural language and translate it into modifications for the project's PCP module specifications and acceptance criteria.

You are given the current program objective, strategy decomposition, and list of existing modules with their specs.
Analyze which module (or if a new module needs to be created) is responsible for this feature, and generate the updated or new spec and acceptance criteria.
Ensure that new acceptance criteria IDs do not conflict with existing ones (e.g. if the module already has A001, start new ones at A002).

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "module_action": "modify | create",
  "module_name": "module-name",
  "explanation": "A plain-English summary of what will be built and why, detailing how this intent satisfies the request.",
  "spec_changes": {
    "version": "2.0",
    "module": "module-name",
    "description": "Description of the module including the new features (minimum 10 words).",
    "objective_coverage": ["Explain how this module covers objective.md objectives"],
    "dependencies": ["dependency-module-name"],
    "constraints": [],
    "build_vs_buy": {
      "decision": "not_applicable",
      "rationale": "Pure business-logic module -- no whole-module tool-adoption choice; see per-criterion build_vs_buy instead.",
      "candidates_considered": []
    }
  },
  "acceptance_changes": {
    "version": "2.0",
    "module": "module-name",
    "criteria": [
      {
        "id": "A002",
        "description": "Clear description of the new exit criterion",
        "check": "manual | ast_pattern | test_passes | file_exists",
        "status": "pending",
        "logic_tier": 6,
        "build_vs_buy": {
          "decision": "build_fresh",
          "rationale": "Why this decision, one sentence.",
          "candidates_considered": []
        }
      }
    ]
  }
}

Every NEW criterion MUST declare logic_tier (1-6, the cheapest rung that correctly makes this decision: 1=deterministic, 2=optimization/solver, 3=statistical/ML, 4=RAG, 5=cached reuse, 6=deep-think LLM -- default to the cheapest rung that genuinely fits, do not default everything to 6) and build_vs_buy: {decision, rationale, candidates_considered} where decision is exactly one of: reuse_whole, reuse_partial (vendor one file/function, not the whole repo), reimplement_from_reference (study a solved approach and write original code, no code copied), fork_adapt, build_fresh. If this feature touches an infrastructure-shaped module (portal, auth, integrations, orchestration engine), spec_changes ALSO needs a real module-level build_vs_buy decision instead of 'not_applicable'.
"""


def _load_project_context(pcp_dir: Path) -> str:
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    decomposition = (pcp_dir / "strategy" / "decomposition.md").read_text() if (pcp_dir / "strategy" / "decomposition.md").exists() else ""

    parts = [
        f"## Program Objective\n{objective}\n",
        f"## Strategy Decomposition\n{decomposition}\n",
        "## Existing Modules Specs\n"
    ]

    modules_dir = pcp_dir / "strategy" / "modules"
    if modules_dir.exists():
        for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
            mod_name = spec_path.parent.name
            spec_content = spec_path.read_text()
            acc_content = ""
            acc_path = spec_path.parent / "acceptance.yaml"
            if acc_path.exists():
                acc_content = acc_path.read_text()
            parts.append(
                f"### Module: {mod_name}\n"
                f"#### spec.yaml:\n```yaml\n{spec_content}```\n"
                f"#### acceptance.yaml:\n```yaml\n{acc_content}```\n"
            )

    return "\n".join(parts)


@click.command()
@click.argument("intent")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def pm(intent: str, project_path: str | None):
    """Translate natural language intent to spec/acceptance criteria modifications."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    context_str = _load_project_context(pcp_dir)
    user_prompt = f"## Intent\n{intent}\n\n{context_str}"

    max_context_chars = _max_context_chars()
    if len(user_prompt) > max_context_chars:
        console.print(
            f"[red]Error:[/red] assembled project context is {len(user_prompt):,} chars, "
            f"over the {max_context_chars:,}-char pm limit."
        )
        console.print(
            "[dim]This project's specs have grown too large to paste in full on every `pcp pm` call. "
            "Not truncated automatically -- a silent cut could drop an unrelated module's constraints "
            "the new intent actually depends on. Consider splitting into smaller modules, or raise "
            "PCP_PM_MAX_CONTEXT_CHARS if you've confirmed the full context is genuinely needed.[/dim]"
        )
        sys.exit(2)

    console.print("[dim]Analyzing intent against project context...[/dim]")

    try:
        result = llm.call_json(SYSTEM_PROMPT, user_prompt, pcp_dir=pcp_dir, command="pm")
    except RuntimeError as e:
        console.print(f"[red]Error calling LLM:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    action = result.get("module_action", "modify")
    mod_name = result.get("module_name", "").strip().lower()
    explanation = result.get("explanation", "")

    if not mod_name:
        console.print("[red]Error: LLM did not generate a module name.[/red]")
        sys.exit(2)

    console.print(f"\n[bold]Planned Action:[/bold] {action.upper()} module [cyan]'{mod_name}'[/cyan]")
    console.print(f"[dim]{explanation}[/dim]\n")

    # Display proposed changes
    console.print("[bold]Proposed spec.yaml changes:[/bold]")
    console.print(yaml.dump(result["spec_changes"], default_flow_style=False))
    console.print("[bold]Proposed acceptance.yaml criteria to add:[/bold]")
    for c in result["acceptance_changes"].get("criteria", []):
        console.print(f"  - [{c['id']}] {c['description']} (check: {c.get('check', 'manual')})")

    if not click.confirm("\nApprove these changes and queue them for build?"):
        console.print("[yellow]Changes aborted.[/yellow]")
        sys.exit(0)

    mod_dir = pcp_dir / "strategy" / "modules" / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    spec_path = mod_dir / "spec.yaml"
    acc_path = mod_dir / "acceptance.yaml"

    # Force version 2.0 regardless of what the LLM returned -- same reasoning
    # as kickoff.py: a spec pm touches must always get logic_tier/build_vs_buy
    # enforcement, never silently stay on (or revert to) the ungated 1.0 shape.
    spec_changes = result["spec_changes"]
    spec_changes["version"] = "2.0"

    # On modify, a real prior module-level build_vs_buy decision must not be
    # silently discarded just because this pm call's response omitted it --
    # only coerce to a flagged placeholder if one never existed.
    existing_spec = {}
    if spec_path.exists():
        try:
            existing_spec = yaml.safe_load(spec_path.read_text()) or {}
        except Exception:
            pass
    if "build_vs_buy" not in spec_changes and existing_spec.get("build_vs_buy"):
        spec_changes["build_vs_buy"] = existing_spec["build_vs_buy"]

    coercion_warnings = _normalize_spec(spec_changes, mod_name)
    spec_path.write_text(yaml.dump(spec_changes, default_flow_style=False))

    # Save/Merge acceptance.yaml
    existing_criteria = []
    if acc_path.exists():
        try:
            acc_data = yaml.safe_load(acc_path.read_text()) or {}
            existing_criteria = acc_data.get("criteria", [])
        except Exception:
            pass

    # Create mapping of existing by ID
    criteria_map = {c["id"]: c for c in existing_criteria}

    # Add/Merge new ones
    for new_c in result["acceptance_changes"].get("criteria", []):
        criteria_map[new_c["id"]] = new_c

    merged_acceptance = {
        "version": "2.0",
        "module": mod_name,
        "criteria": sorted(list(criteria_map.values()), key=lambda x: x["id"])
    }
    # Coerces the WHOLE merged list, not just the new criteria -- retroactively
    # upgrades any pre-existing criterion (e.g. from an old 1.0-era module)
    # that's missing logic_tier/build_vs_buy the first time pm touches it.
    coercion_warnings += _normalize_acceptance(merged_acceptance, mod_name)

    acc_path.write_text(yaml.dump(merged_acceptance, default_flow_style=False))

    console.print(f"[green]✓[/green] Module '{mod_name}' spec and acceptance criteria updated.")
    if coercion_warnings:
        console.print(f"[yellow]⚠  {len(coercion_warnings)} field(s) didn't match the schema, coerced to a safe default:[/yellow]")
        for w in coercion_warnings:
            console.print(f"   {w}")

    # Refresh current state & pcp.md snapshot
    from pcp.commands.scan import scan
    ctx = click.get_current_context(silent=True)
    if ctx:
        ctx.invoke(scan, project_path=str(pcp_dir.parent), quiet=True)
    else:
        # Fallback to direct call logic
        from datetime import datetime, timezone
        from pcp.commands.scan import _scan_module, _write_current_state, _load_prior_manual_status
        modules_dir = get_modules_dir(pcp_dir)
        prior_manual = _load_prior_manual_status(pcp_dir / "current_state.md")
        modules_results = []
        for af in sorted(modules_dir.glob("*/acceptance.yaml")):
            m_name = af.parent.name
            res = _scan_module(m_name, af, pcp_dir.parent, prior_manual)
            modules_results.append(res)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_current_state(pcp_dir, modules_results, timestamp)
        total = sum(len(m["criteria"]) for m in modules_results)
        complete = sum(1 for m in modules_results for c in m["criteria"] if c["status"] == "complete")
        write_pcp_md(pcp_dir, modules_results, timestamp, total, complete)

    console.print("[green]✓[/green] Project state refreshed. Run [cyan]pcp build[/cyan] to begin development.")
