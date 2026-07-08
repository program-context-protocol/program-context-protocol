"""pcp kickoff — vision → strategy generation via LLM."""

import sys
import json
from pathlib import Path
import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir, get_modules_dir
from pcp.schema.validator import validate_file
from pcp.llm import client as llm
from pcp.commands.init import ADR_EXAMPLE, DOMAIN_KB_TEMPLATE
from pcp.commands.validate_strategy import (
    _build_user_prompt as build_val_prompt,
    SYSTEM_PROMPT as VAL_SYSTEM_PROMPT,
    _render_results as render_val_results
)
from pcp.pcp_status import write_pcp_md

console = Console()

SYSTEM_PROMPT = """\
You are an expert product manager and software architect.
Your task is to take a product vision document (in plain English) and decompose it into a structured set of program modules and SDLC phases using the PCP (Program Context Protocol) design system.

Decompose the vision into modules. Each module must cover a distinct set of features/requirements.
The strategy decomposition must detail how these modules cover the objective.
Also generate acceptance criteria for each module (e.g. A001, A002) with clear descriptions.

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "objective": "# Program Objective\\n\\n## Why This Exists\\n[Explain why]\\n\\n## What Success Looks Like\\n1. [Outcome 1]\\n\\n## Out of Scope\\n- [Out of scope items]",
  "target_state": "# Target State\\n\\n[Ideal end state]",
  "architecture": "# Architecture\\n\\n## Tech Stack\\n| Layer | Choice | Why |\\n|---|---|---|\\n| Backend | Python/FastAPI | ... |\\n\\n## Key Constraints\\n- [Constraint]",
  "decomposition": "# Strategy Decomposition\\n\\n## How the Objective Breaks Down\\n[Explanation]\\n\\n## Module Dependency Order\\n1. [module-a] - reason",
  "sdlc_phase": {
    "version": "1.0",
    "current_phase": "planning",
    "phases": [
      {
        "name": "planning",
        "exit_criteria": [
          {
            "id": "E001",
            "description": "Strategy decomposition approved by PM",
            "check": "manual",
            "status": "pending"
          }
        ]
      },
      {
        "name": "alpha",
        "exit_criteria": [
          {
            "id": "E001",
            "description": "Core features implemented",
            "check": "manual",
            "status": "pending"
          }
        ]
      }
    ]
  },
  "modules": [
    {
      "name": "module-name",
      "spec": {
        "version": "1.0",
        "module": "module-name",
        "description": "Short description of what the module does (at least 10 words).",
        "objective_coverage": ["What part of objective.md is covered"],
        "dependencies": [],
        "constraints": []
      },
      "acceptance": {
        "version": "1.0",
        "module": "module-name",
        "criteria": [
          {
            "id": "A001",
            "description": "Description of exit criterion",
            "check": "manual",
            "status": "pending"
          }
        ]
      }
    }
  ],
  "_comment_criteria_enums": "Every criterion's check MUST be exactly one of: ast_pattern, file_exists, test_passes, manual, dom_contains, url_responds, visual. Every criterion's status MUST be exactly one of: pending, complete, deferred, blocked-ci, blocked-secret, blocked-regression. Do not invent other values (e.g. 'automated' or 'done') even if they seem descriptive -- these are the only ones a validator will accept. When generating a strategy from a vision doc (not yet built), every criterion's status should be 'pending' unless the vision explicitly states something is already implemented.",
  "ci_rules": {
    "version": "1.0",
    "rules": [
      {
        "id": "R001",
        "name": "No hardcoded secrets",
        "check": "ast_pattern",
        "pattern": "(password|secret|api_key)\\\\s*=\\\\s*['\\\"][^'\\\"]{8,}['\\\"]",
        "severity": "hard_block"
      }
    ]
  },
  "architect_persona": "# Architect Persona\\n\\n## Principles I Enforce\\n- [Enforced principles]\\n\\n## Anti-Patterns I Block (BLOCK severity)\\n- [Blocked anti-patterns]\\n\\n## Patterns I Warn About (WARN severity)\\n- [Warnings]"
}
"""


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


VALID_CHECKS = {"ast_pattern", "file_exists", "test_passes", "manual", "dom_contains", "url_responds", "visual"}
VALID_STATUSES = {"pending", "complete", "deferred", "blocked-ci", "blocked-secret", "blocked-regression"}
# Best-effort mapping for common LLM-invented values that don't match the
# closed schema enum but have an obvious intended meaning.
_STATUS_ALIASES = {"done": "complete", "finished": "complete", "in_progress": "pending", "todo": "pending"}
_CHECK_ALIASES = {"automated": "manual", "auto": "manual", "unit_test": "test_passes", "integration_test": "test_passes"}


def _normalize_acceptance(acceptance: dict, module_name: str) -> list[str]:
    """Coerces check/status values outside the schema's closed enum to a
    safe default in place, returns a list of human-readable warnings for
    anything it had to coerce. Found necessary 2026-07-08: kickoff's LLM
    generation invented plausible-but-invalid values ('automated', 'done')
    for a real, more complex vision doc -- validate_file was imported here
    but never actually called, so these silently reached disk and only
    surfaced later, opaquely, whenever `pcp scan` happened to run next."""
    warnings = []
    for c in acceptance.get("criteria", []):
        check = c.get("check")
        if check not in VALID_CHECKS:
            fixed = _CHECK_ALIASES.get(check, "manual")
            warnings.append(f"{module_name}/{c.get('id', '?')}: check '{check}' is not valid, coerced to '{fixed}'")
            c["check"] = fixed
        status = c.get("status")
        if status not in VALID_STATUSES:
            fixed = _STATUS_ALIASES.get(status, "pending")
            warnings.append(f"{module_name}/{c.get('id', '?')}: status '{status}' is not valid, coerced to '{fixed}'")
            c["status"] = fixed
    return warnings


@click.command()
@click.argument("vision_file", type=click.Path(exists=True))
@click.option("--path", "project_path", type=click.Path(), default=".",
              help="Project root (default: current directory).")
@click.option("--force", is_flag=True, help="Force overwrite existing .pcp/ directory.")
def kickoff(vision_file: str, project_path: str, force: bool):
    """Kick off a new project from a product vision document."""
    root = Path(project_path).resolve()
    pcp_dir = root / ".pcp"

    if pcp_dir.exists() and not force:
        if not click.confirm("An existing .pcp/ directory was found. This kickoff will overwrite it. Proceed?"):
            console.print("[yellow]Kickoff aborted.[/yellow]")
            sys.exit(0)

    vision_path = Path(vision_file).resolve()
    try:
        vision_content = vision_path.read_text()
    except Exception as e:
        console.print(f"[red]Error reading vision file:[/red] {e}")
        sys.exit(2)

    console.print("[dim]Analyzing vision and generating Strategy decomposition...[/dim]")

    try:
        result = llm.call_json(SYSTEM_PROMPT, vision_content, pcp_dir=pcp_dir, command="kickoff")
    except RuntimeError as e:
        console.print(f"[red]Error calling LLM:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    # Write files
    _write_file(pcp_dir / "objective.md", result["objective"])
    _write_file(pcp_dir / "target_state.md", result["target_state"])
    _write_file(pcp_dir / "architecture.md", result["architecture"])
    _write_file(pcp_dir / "strategy" / "decomposition.md", result["decomposition"])

    sdlc_yaml = yaml.dump(result["sdlc_phase"], default_flow_style=False)
    _write_file(pcp_dir / "SDLC_phase.yaml", sdlc_yaml)

    ci_yaml = yaml.dump(result["ci_rules"], default_flow_style=False)
    _write_file(pcp_dir / "ci_rules.yaml", ci_yaml)

    _write_file(pcp_dir / "architect_persona.md", result["architect_persona"])
    _write_file(pcp_dir / "kb" / "adr" / "ADR-001-example.md", ADR_EXAMPLE)
    _write_file(pcp_dir / "kb" / "domain" / "general.md", DOMAIN_KB_TEMPLATE)

    # Write module specs and acceptance criteria
    coercion_warnings = []
    for m in result.get("modules", []):
        mod_dir = pcp_dir / "strategy" / "modules" / m["name"]
        coercion_warnings += _normalize_acceptance(m["acceptance"], m["name"])
        _write_file(mod_dir / "spec.yaml", yaml.dump(m["spec"], default_flow_style=False))
        _write_file(mod_dir / "acceptance.yaml", yaml.dump(m["acceptance"], default_flow_style=False))

    console.print("[green]✓[/green] Generated PCP files under [cyan].pcp/[/cyan]")

    if coercion_warnings:
        console.print(f"[yellow]⚠  {len(coercion_warnings)} criterion field(s) didn't match the schema, coerced to a safe default:[/yellow]")
        for w in coercion_warnings:
            console.print(f"   {w}")

    # Schema-validate what actually landed on disk -- advisory, matches
    # scan.py's own posture (warn, don't block), but at least surfaces any
    # remaining issue right here instead of only on the next `pcp scan`.
    for m in result.get("modules", []):
        mod_dir = pcp_dir / "strategy" / "modules" / m["name"]
        errors = validate_file(mod_dir / "acceptance.yaml", "module_acceptance")
        if errors:
            console.print(f"[yellow]⚠  {m['name']}/acceptance.yaml still has schema issues after coercion:[/yellow]")
            for e in errors:
                console.print(f"   {e}")

    # Run validate-strategy automatically
    console.print("\n[bold]Running validate-strategy...[/bold]")
    objective = result["objective"]
    decomposition = result["decomposition"]
    modules = {m["name"]: m["spec"] for m in result.get("modules", [])}

    val_user_prompt = build_val_prompt(objective, decomposition, modules)
    try:
        val_result = llm.call_json(
            VAL_SYSTEM_PROMPT, val_user_prompt,
            model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="kickoff-validate",
        )
    except Exception as e:
        console.print(f"[yellow]Warning: Could not run validate-strategy automatically: {e}[/yellow]")
        val_result = None

    if val_result:
        render_val_results(pcp_dir, val_result, output_json=False)

    # PM approval step
    if click.confirm("\nApprove this strategy and proceed to alpha phase?"):
        # Update SDLC phase to alpha
        sdlc_data = result["sdlc_phase"]
        sdlc_data["current_phase"] = "alpha"
        for p in sdlc_data.get("phases", []):
            if p["name"] == "planning":
                for c in p.get("exit_criteria", []):
                    if c["id"] == "E001":
                        c["status"] = "complete"

        _write_file(pcp_dir / "SDLC_phase.yaml", yaml.dump(sdlc_data, default_flow_style=False))

        # Reconstruct modules results format for write_pcp_md
        modules_results = []
        for m in result.get("modules", []):
            criteria = []
            for c in m["acceptance"].get("criteria", []):
                criteria.append({
                    "id": c["id"],
                    "description": c["description"],
                    "check": c.get("check", "manual"),
                    "status": c.get("status", "pending")
                })
            modules_results.append({
                "module": m["name"],
                "criteria": criteria
            })

        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        total = sum(len(m["criteria"]) for m in modules_results)
        complete = sum(1 for m in modules_results for c in m["criteria"] if c["status"] == "complete")

        pcp_md_path = write_pcp_md(pcp_dir, modules_results, timestamp, total, complete)
        console.print(f"\n[green]Strategy approved! Transitioned current phase to: alpha.[/green]")
        console.print(f"Governance snapshot written to [cyan]{pcp_md_path.name}[/cyan]")
    else:
        console.print("\n[yellow]Strategy rejected. Spec files kept in .pcp/ for your manual modification.[/yellow]")
