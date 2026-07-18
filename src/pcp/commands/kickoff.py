"""pcp kickoff — vision → strategy generation via LLM."""

import os
import re
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
        "version": "2.0",
        "module": "module-name",
        "description": "Short description of what the module does (at least 10 words).",
        "objective_coverage": ["What part of objective.md is covered"],
        "dependencies": [],
        "constraints": [],
        "build_vs_buy": {
          "decision": "not_applicable",
          "rationale": "Pure business-logic module -- no whole-module tool-adoption choice; see per-criterion build_vs_buy instead.",
          "candidates_considered": []
        }
      },
      "acceptance": {
        "version": "2.0",
        "module": "module-name",
        "criteria": [
          {
            "id": "A001",
            "description": "Description of exit criterion",
            "check": "manual",
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
  ],
  "_comment_criteria_enums": "Every criterion's check MUST be exactly one of: ast_pattern, file_exists, test_passes, manual, dom_contains, url_responds, visual. Every criterion's status MUST be exactly one of: pending, complete, deferred, blocked-ci, blocked-secret, blocked-regression. Do not invent other values (e.g. 'automated' or 'done') even if they seem descriptive -- these are the only ones a validator will accept. When generating a strategy from a vision doc (not yet built), every criterion's status should be 'pending' unless the vision explicitly states something is already implemented.",
  "_comment_logic_tier": "Every criterion MUST declare logic_tier (1-6). Choose by classifying the CORRECTNESS ORACLE, not the task: correctness = 'satisfies rules I can write down completely' -> 1 (litmus: could you write unit tests asserting EXACT outputs for every input class right now?); correctness = 'best feasible option under known constraints' -> 2 (litmus: enumerable constraints + a writable objective function -- OR-Tools/CBC); correctness = 'matches historical outcomes' -> 3 (litmus: hundreds of labeled rows exist or are cheap to collect); correctness = 'faithful to what our documents actually say' -> 4 (answer already exists as text in a bounded corpus); 'same as last time for same question' -> 5 (an overlay on other rungs, rarely a destination); correctness = 'a reasonable human would accept it, and another might accept a different answer' -> 6, ONLY after 1-4 each failed for a stated reason. DECOMPOSE FIRST: a criterion is rarely one decision -- classify each decision point, declare the highest rung actually present. Judgment verbs (recommend/interpret/assess) in a description contradict a rung-1 declaration. Do not default everything to 6.",
  "_comment_build_vs_buy": "Every criterion MUST also declare build_vs_buy: {decision, rationale, candidates_considered}. decision is exactly one of: reuse_whole (take an existing package/repo as a dependency), reuse_partial (vendor one file/function/module out of a larger repo), reimplement_from_reference (study a solved approach -- possibly GPL/AGPL, possibly another language -- and write original code implementing the same logic, no code copied), fork_adapt (fork a whole repo, continuously modify), build_fresh (nothing comparable exists). Each infrastructure-shaped module (portal, auth, integrations, orchestration engine) ALSO gets a module-level build_vs_buy in its spec -- pure business-logic modules use 'not_applicable' there since the per-criterion decisions already cover it.",
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


def _max_vision_chars() -> int:
    """Reject-loud, not truncate-silent: unlike capture.py's transcript cap
    (where "most recent" is a sane proxy for "most relevant"), a vision doc
    has no such structure -- a blind truncation could quietly drop the
    important part and generate a wrong strategy with no visible sign why.
    A function, not a module-level constant, so PCP_KICKOFF_MAX_VISION_CHARS
    is read live at call time rather than frozen at import time."""
    return int(os.environ.get("PCP_KICKOFF_MAX_VISION_CHARS", "40000"))


VALID_CHECKS = {"ast_pattern", "file_exists", "test_passes", "manual", "dom_contains", "url_responds", "visual"}
VALID_STATUSES = {"pending", "complete", "deferred", "blocked-ci", "blocked-secret", "blocked-regression"}
VALID_LOGIC_TIERS = {1, 2, 3, 4, 5, 6}
VALID_BVB_DECISIONS = {"reuse_whole", "reuse_partial", "reimplement_from_reference", "fork_adapt", "build_fresh"}
VALID_MODULE_BVB_DECISIONS = VALID_BVB_DECISIONS | {"not_applicable"}
# Best-effort mapping for common LLM-invented values that don't match the
# closed schema enum but have an obvious intended meaning.
_STATUS_ALIASES = {"done": "complete", "finished": "complete", "in_progress": "pending", "todo": "pending"}
_CHECK_ALIASES = {"automated": "manual", "auto": "manual", "unit_test": "test_passes", "integration_test": "test_passes"}

# ci_rules.yaml's own check/severity enums are DIFFERENT from module_acceptance's
# above -- found 2026-07-09, same class of bug as the acceptance.yaml one this
# file already fixed on 2026-07-08, just never applied to ci_rules.yaml: kickoff
# wrote result["ci_rules"] straight to disk with zero validation, so an
# LLM-invented check type ('file_pair_diff', 'grep') or severity ('warn' instead
# of 'advisory') silently reached disk and hard-blocked every future commit via
# pcp check's schema validation -- confirmed live in a real kicked-off project
# (agentberg), not hypothetical.
VALID_CI_CHECKS = {"ast_pattern", "file_exists", "llm_semantic", "protected_path"}
VALID_CI_SEVERITIES = {"hard_block", "advisory"}
_CI_SEVERITY_ALIASES = {"warn": "advisory", "warning": "advisory", "block": "hard_block", "error": "hard_block", "critical": "hard_block"}
# grep-as-string-search is exactly what ast_pattern already is -- a clean,
# safe re-mapping since these rules already carry a real `pattern` field.
# Anything else (e.g. 'file_pair_diff' -- a cross-file consistency check with
# no deterministic equivalent in this schema) falls back to llm_semantic:
# advisory, not silently dropped, and genuinely the closest honest fit.
_CI_CHECK_ALIASES = {"grep": "ast_pattern", "regex": "ast_pattern"}
_CI_ID_PATTERN = re.compile(r"^[A-Z]+_?[0-9]+$")


def _coerce_build_vs_buy(bvb, module_name: str, criterion_id: str, valid_decisions: set) -> tuple[dict, list[str]]:
    """Coerces a build_vs_buy block to a schema-valid shape. Missing or
    malformed input is never silently dropped -- it's replaced with an
    explicitly-flagged placeholder (build_fresh/not-specified) so a missing
    deliberation is visibly a coercion, not indistinguishable from a real
    build_fresh decision someone actually made."""
    warnings = []
    if not isinstance(bvb, dict) or "decision" not in bvb or "rationale" not in bvb:
        warnings.append(f"{module_name}/{criterion_id}: build_vs_buy missing or malformed, coerced to a flagged placeholder")
        return {
            "decision": "build_fresh",
            "rationale": "Not specified by generator -- coerced placeholder, review before treating as a real decision.",
            "candidates_considered": [],
        }, warnings
    decision = bvb.get("decision")
    if decision not in valid_decisions:
        warnings.append(f"{module_name}/{criterion_id}: build_vs_buy decision '{decision}' is not valid, coerced to 'build_fresh'")
        bvb = {**bvb, "decision": "build_fresh"}
    bvb.setdefault("candidates_considered", [])
    return bvb, warnings


def _normalize_acceptance(acceptance: dict, module_name: str) -> list[str]:
    """Coerces check/status/logic_tier/build_vs_buy values outside the
    schema's closed enums to a safe default in place, returns a list of
    human-readable warnings for anything it had to coerce. Found necessary
    2026-07-08: kickoff's LLM generation invented plausible-but-invalid
    values ('automated', 'done') for a real, more complex vision doc --
    validate_file was imported here but never actually called, so these
    silently reached disk and only surfaced later, opaquely, whenever
    `pcp scan` happened to run next. logic_tier/build_vs_buy get the same
    treatment from day one rather than repeating that gap."""
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
        tier = c.get("logic_tier")
        if tier not in VALID_LOGIC_TIERS:
            warnings.append(f"{module_name}/{c.get('id', '?')}: logic_tier '{tier}' is not valid, coerced to 6 (deep-think -- safest default when unknown)")
            c["logic_tier"] = 6
        bvb, bvb_warnings = _coerce_build_vs_buy(c.get("build_vs_buy"), module_name, c.get("id", "?"), VALID_BVB_DECISIONS)
        c["build_vs_buy"] = bvb
        warnings += bvb_warnings
    return warnings


def _normalize_spec(spec: dict, module_name: str) -> list[str]:
    """Coerces a module spec's module-level build_vs_buy (infrastructure-
    shaped modules -- portal, auth, integrations, orchestration engine) the
    same way _normalize_acceptance coerces per-criterion fields."""
    bvb, warnings = _coerce_build_vs_buy(spec.get("build_vs_buy"), module_name, "(module-level)", VALID_MODULE_BVB_DECISIONS)
    spec["build_vs_buy"] = bvb
    return warnings


def _normalize_ci_rules(ci_rules: dict) -> list[str]:
    """Coerces ci_rules.yaml's check/severity/id values outside the schema's
    closed enums to a safe default in place, same posture as
    _normalize_acceptance: a placeholder-mismatch gets fixed and flagged, not
    silently written to disk to hard-block every future commit via pcp
    check's schema validation the first time someone tries to ship."""
    warnings = []
    seen_ids: set[str] = set()
    for i, r in enumerate(ci_rules.get("rules", [])):
        rid_display = r.get("id", f"rule#{i}")

        check = r.get("check")
        if check not in VALID_CI_CHECKS:
            fixed = _CI_CHECK_ALIASES.get(check)
            if fixed is None:
                fixed = "llm_semantic"
            warnings.append(f"ci_rules/{rid_display}: check '{check}' is not valid, coerced to '{fixed}'")
            r["check"] = fixed

        # Whichever check type it ends up as, make sure the field that type
        # actually requires exists -- coercing the type alone without this
        # would just trade one schema error for another.
        if r["check"] == "llm_semantic" and not r.get("description"):
            # Prefer `pattern` over `name`: when a check type gets coerced
            # away from ast_pattern/grep/file_pair_diff, `pattern` often
            # held free-text semantic explanation (not a real regex) --
            # that's more useful as `description` than the terse rule name.
            r["description"] = r.get("pattern") or r.get("name") or "no description provided"
        elif r["check"] == "ast_pattern" and not r.get("pattern"):
            # Can't safely claim ast_pattern with no pattern to match -- fall
            # back to the one type that only needs a description.
            r["check"] = "llm_semantic"
            r.setdefault("description", r.get("name") or "no description provided")
            warnings.append(f"ci_rules/{rid_display}: ast_pattern with no pattern field, coerced to llm_semantic instead")
        elif r["check"] == "file_exists" and not r.get("target"):
            r["check"] = "llm_semantic"
            r.setdefault("description", r.get("name") or "no description provided")
            warnings.append(f"ci_rules/{rid_display}: file_exists with no target field, coerced to llm_semantic instead")
        elif r["check"] == "protected_path" and not r.get("scope"):
            r["check"] = "llm_semantic"
            r.setdefault("description", r.get("name") or "no description provided")
            warnings.append(f"ci_rules/{rid_display}: protected_path with no scope field, coerced to llm_semantic instead")

        severity = r.get("severity")
        if severity not in VALID_CI_SEVERITIES:
            fixed = _CI_SEVERITY_ALIASES.get(severity, "advisory")
            warnings.append(f"ci_rules/{rid_display}: severity '{severity}' is not valid, coerced to '{fixed}'")
            r["severity"] = fixed

        rid = r.get("id", "")
        if not rid or not _CI_ID_PATTERN.match(rid) or rid in seen_ids:
            fixed_id = f"GEN_{i + 1:03d}"
            warnings.append(f"ci_rules: id '{rid or '(missing)'}' is not valid or duplicate, coerced to '{fixed_id}'")
            r["id"] = fixed_id
            rid = fixed_id
        seen_ids.add(rid)

        if not r.get("name"):
            r["name"] = rid_display

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

    max_vision_chars = _max_vision_chars()
    if len(vision_content) > max_vision_chars:
        console.print(
            f"[red]Error:[/red] {vision_file} is {len(vision_content):,} chars, "
            f"over the {max_vision_chars:,}-char kickoff limit."
        )
        console.print(
            "[dim]Split it into a shorter vision doc, or scope this kickoff to one phase at a time — "
            "not truncated automatically, since a vision doc has no 'most recent = most relevant' "
            "structure the way a session transcript does, so a silent cut could quietly drop the "
            "important part and generate a wrong strategy with no visible sign why.[/dim]"
        )
        sys.exit(2)

    console.print("[dim]Analyzing vision and generating Strategy decomposition...[/dim]")

    try:
        # Sonnet is the reviewed default for generation calls (see
        # llm/client.py's model-selection strategy) -- replaces the prior
        # ambiguous "inherited/default" (whatever the CLI's own default
        # happened to be). PCP_MODEL still overrides for a human debugging.
        result = llm.call_json(SYSTEM_PROMPT, vision_content, model=llm.BUILD_MODEL, pcp_dir=pcp_dir, command="kickoff")
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

    # Force the one valid literal version regardless of what the LLM returned
    # -- there's no v1/v2 duality for ci_rules.yaml the way there is for
    # module specs, so this is zero-ambiguity, same defensive posture as
    # forcing "2.0" on acceptance.yaml/spec.yaml below.
    result["ci_rules"]["version"] = "1.0"
    coercion_warnings = _normalize_ci_rules(result["ci_rules"])
    ci_yaml = yaml.dump(result["ci_rules"], default_flow_style=False)
    _write_file(pcp_dir / "ci_rules.yaml", ci_yaml)

    _write_file(pcp_dir / "architect_persona.md", result["architect_persona"])
    _write_file(pcp_dir / "kb" / "adr" / "ADR-001-example.md", ADR_EXAMPLE)
    _write_file(pcp_dir / "kb" / "domain" / "general.md", DOMAIN_KB_TEMPLATE)

    # Write module specs and acceptance criteria
    for m in result.get("modules", []):
        mod_dir = pcp_dir / "strategy" / "modules" / m["name"]
        # Force version 2.0 regardless of what the LLM returned -- a fresh
        # kickoff must always get logic_tier/build_vs_buy enforcement, not
        # silently downgrade to the ungated 1.0 shape on a generation slip.
        m["spec"]["version"] = "2.0"
        m["acceptance"]["version"] = "2.0"
        coercion_warnings += _normalize_spec(m["spec"], m["name"])
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
    # remaining issue right here instead of only the first time someone
    # tries to commit and pcp check's Layer 1 schema validation hard-blocks
    # on it -- confirmed live in a real kicked-off project (agentberg) before
    # this check existed.
    ci_rules_errors = validate_file(pcp_dir / "ci_rules.yaml", "ci_rules")
    if ci_rules_errors:
        console.print("[yellow]⚠  ci_rules.yaml still has schema issues after coercion:[/yellow]")
        for e in ci_rules_errors:
            console.print(f"   {e}")

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
