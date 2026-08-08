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
from pcp.commands.kickoff import (
    _normalize_acceptance, _normalize_spec, check_capability_coverage,
    check_capability_criterion_coverage,
    check_module_logic_breakdown_coverage, check_prior_art_evidence,
)
from pcp.commands.validate_strategy import (
    _build_user_prompt as build_val_prompt,
    SYSTEM_PROMPT as VAL_SYSTEM_PROMPT,
    _render_results as render_val_results,
)

console = Console()

def _max_context_chars() -> int:
    """Reject-loud, not truncate-silent -- same posture as kickoff.py's
    vision-doc guard. Bounds _load_project_context's assembled prompt (every
    existing module's spec.yaml + acceptance.yaml). A function, not a
    module-level constant, so PCP_PM_MAX_CONTEXT_CHARS is read live at call
    time rather than frozen at import time.

    Default raised 60,000 -> 400,000 (2026-07-29). 60k chars is ~15k tokens --
    that guard was not measuring "this project no longer fits in a context
    window", it was firing on ordinary project size. Measured across the 8
    local PCP-managed projects, 4 of 8 exceeded it (Project O 392k,
    Project W 94k, Project M 68k, Project G 43k), so `pcp pm` was dead on half
    the fleet with an error suggesting the fix was to "split into smaller
    modules" -- i.e. restructure a healthy 27-module project to satisfy an
    arbitrary constant. 400k chars is ~100k tokens, half a 200k window, which
    is what the guard should actually be protecting."""
    return int(os.environ.get("PCP_PM_MAX_CONTEXT_CHARS", "400000"))


# Fields on an EXISTING criterion that pm demonstrably never reads.
# pm's job is: route an intent to module(s), then emit new criteria. It needs
# the existing IDs (collision avoidance), descriptions (don't re-add what
# exists), and the scheduling fields it must itself populate. It does not need
# other criteria's build_vs_buy rationales, design_justification memos, QA
# evidence, or verifier notes -- and on Project O those four fields
# alone were 111k of the 341k spec+acceptance payload (build_vs_buy 74k,
# design_justification 28k, test 8.7k, notes/verified_by/pattern 3.5k).
#
# This is a projection, not a truncation: whole fields are dropped by name, so
# nothing is cut mid-sentence and no module's constraints or dependencies are
# lost. Module-level spec.yaml stays verbatim for every module, which is where
# the cross-module constraints the old error message worried about actually
# live.
_PM_CRITERION_KEEP_FIELDS = (
    "id", "description", "check", "status",
    "logic_tier", "depends_on", "target", "pattern",
)


def _slim_acceptance(acc_text: str) -> str:
    """Project an acceptance.yaml down to the fields pm uses.

    Fails OPEN: any parse problem returns the original text unchanged rather
    than risk handing the LLM a mangled spec."""
    try:
        data = yaml.safe_load(acc_text) or {}
        criteria = data.get("criteria")
        if not isinstance(criteria, list):
            return acc_text
        slim = {
            "version": data.get("version"),
            "module": data.get("module"),
            "criteria": [
                {k: c[k] for k in _PM_CRITERION_KEEP_FIELDS if k in c}
                for c in criteria if isinstance(c, dict)
            ],
        }
        return yaml.dump(slim, default_flow_style=False, sort_keys=False)
    except Exception:
        return acc_text

SYSTEM_PROMPT = """\
You are an expert product manager.
Your task is to take a feature intent expressed in natural language and translate it into modifications for the project's PCP module specifications and acceptance criteria.

You are given the current program objective, strategy decomposition, and list of existing modules with their specs.

DECOMPOSE FIRST, THEN MAP (GUIDE pattern, arXiv:2502.21068 -- the one academically validated fix for LLMs silently dropping requirements during one-shot generation): before deciding which module(s) this intent touches, populate `capabilities_enumerated` with EVERY distinct capability/requirement this intent implies, however small. Only after that list is complete, decide which module(s) each capability belongs to.

DECOMPOSE FIRST applies one layer deeper too: if this intent adds real internal complexity to a module (not just one more criterion of the same shape), update that module's `spec_changes.module_logic_breakdown` with the new internal components/sub-flows/edge-cases before writing its new criteria -- derive the criteria from the updated breakdown. Skip this field entirely for a small, same-shape addition that doesn't change the module's actual internal decomposition.

A real feature intent routinely spans MORE THAN ONE existing or new module (e.g. "add payments" may touch billing, notifications, and auth) -- do not force everything into a single module just because the schema used to only allow one. `modules` is a LIST: include one entry per module this intent actually touches, whether that's one module or several. Analyze which module (or modules) are responsible, and for each, generate the updated or new spec and acceptance criteria for that module only.

Ensure that new acceptance criteria IDs do not conflict with existing ones within their own module (e.g. if a module already has A001, its new ones start at A002).

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "capabilities_enumerated": ["Every distinct capability/requirement this intent implies, one per discrete thing -- populate BEFORE deciding modules."],
  "overall_explanation": "A plain-English summary of what will be built and why, across all modules this intent touches.",
  "modules": [
    {
      "module_action": "modify | create",
      "module_name": "module-name",
      "module_explanation": "What this specific module handles for this intent.",
      "spec_changes": {
        "version": "2.0",
        "module": "module-name",
        "description": "Description of the module including the new features (minimum 10 words).",
        "objective_coverage": ["Explain how this module covers objective.md objectives"],
        "module_logic_breakdown": ["Only if this intent adds real internal complexity -- this module's updated internal components/sub-flows/edge-cases. Omit the key entirely for a small, same-shape addition."],
        "category_reference": {
          "category": "OPTIONAL WHOLE FIELD -- only include if a researched category reference (.pcp/strategy/inspiration_art.md, given below if it exists) genuinely covers this module, especially a NEW module created to close a coverage gap. Omit entirely rather than guessing.",
          "source_evidence": ["Cite the researched section, don't invent new evidence"],
          "classification": "adopted|adapted_requirement|adapted_system|custom",
          "rationale": "One sentence"
        },
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
            "check": "manual | ast_pattern | test_passes | file_exists | dom_contains | url_responds | visual",
            "status": "pending",
            "logic_tier": 6,
            "build_vs_buy": {
              "decision": "build_fresh",
              "rationale": "Why this decision, one sentence.",
              "candidates_considered": []
            },
            "depends_on": [],
            "target": "src/path/to/the/file/this/criterion/writes.py"
          }
        ]
      }
    }
  ]
}

Every NEW criterion MUST declare logic_tier (1-6, the cheapest rung that correctly makes this decision: 1=deterministic, 2=optimization/solver, 3=statistical/ML, 4=RAG, 5=cached reuse, 6=deep-think LLM -- default to the cheapest rung that genuinely fits, do not default everything to 6) and build_vs_buy: {decision, rationale, candidates_considered} where decision is exactly one of: reuse_whole, reuse_partial (vendor one file/function, not the whole repo), reimplement_from_reference (study a solved approach and write original code, no code copied), fork_adapt, build_fresh. If a module this intent touches is infrastructure-shaped (portal, auth, integrations, orchestration engine), its spec_changes ALSO needs a real module-level build_vs_buy decision instead of 'not_applicable'.

Every NEW criterion MUST also declare depends_on: a list of OTHER criterion ids (within the same module) that must be built first. Default to an EMPTY list -- most criteria are genuinely independent and should build in parallel. Only list a real id when this criterion's implementation would break or be meaningless without that other one existing first. When genuinely unsure, prefer the empty list -- a false dependency costs real parallelism for nothing.

Every criterion SHOULD also declare `target`: the single primary file path it will create or modify (e.g. "src/storage/upload.py"). This is not documentation -- build.py schedules criterion-level parallelism from it. Two criteria run concurrently, each in its own isolated worktree blind to the other, ONLY when both declare a target and the targets differ; a criterion with no declared target has an unknown file surface and is run alone. Declaring accurate, DISTINCT targets is therefore what buys parallel builds. Two criteria that will genuinely both touch the same file must either declare that same target (so they are serialised) or be split differently -- never leave it blank hoping it works out.

A criterion whose check is dom_contains, url_responds, or visual MUST also declare `url` (the page this check hits once the app is running -- e.g. "/dashboard"), and dom_contains ALSO needs `selector` (a CSS selector or literal text the page must contain). Without `url` the check has nothing to run against and will silently skip forever -- confirmed root cause, 2026-08-03, of a real dogfood project's automated UI checks sitting at 100% skip. Use check: manual instead if the page/route genuinely isn't known yet.
"""


def _load_project_context(pcp_dir: Path) -> tuple[str, int]:
    """Returns (context_string, chars_dropped_by_projection).

    The second value exists so the projection is *visible* -- the whole reason
    the old code pasted everything verbatim was a refusal to cut silently, and
    a projection nobody is told about would repeat that mistake in reverse."""
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    decomposition = (pcp_dir / "strategy" / "decomposition.md").read_text() if (pcp_dir / "strategy" / "decomposition.md").exists() else ""
    inspiration_art_path = pcp_dir / "strategy" / "inspiration_art.md"

    parts = [
        f"## Program Objective\n{objective}\n",
        f"## Strategy Decomposition\n{decomposition}\n",
    ]
    if inspiration_art_path.exists():
        parts.append(
            "## Researched Category Reference Architecture (.pcp/strategy/inspiration_art.md)\n"
            "Use for a new module's category_reference field, and to check whether this intent "
            "reveals a capability a researched category names but no existing module covers.\n"
            f"{inspiration_art_path.read_text()}\n"
        )
    parts.append(
        "## Existing Modules Specs\n"
        "Each module's spec.yaml is verbatim. Each acceptance.yaml lists every "
        "existing criterion, projected to the fields relevant here "
        f"({', '.join(_PM_CRITERION_KEEP_FIELDS)}) -- other criteria's "
        "build_vs_buy/design_justification/QA-evidence fields are omitted as "
        "irrelevant to routing this intent, not because they are absent.\n"
    )

    dropped = 0
    modules_dir = pcp_dir / "strategy" / "modules"
    if modules_dir.exists():
        for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
            mod_name = spec_path.parent.name
            spec_content = spec_path.read_text()
            acc_content = ""
            acc_path = spec_path.parent / "acceptance.yaml"
            if acc_path.exists():
                raw = acc_path.read_text()
                acc_content = _slim_acceptance(raw)
                dropped += max(0, len(raw) - len(acc_content))
            parts.append(
                f"### Module: {mod_name}\n"
                f"#### spec.yaml:\n```yaml\n{spec_content}```\n"
                f"#### acceptance.yaml:\n```yaml\n{acc_content}```\n"
            )

    return "\n".join(parts), dropped


def _write_one_module(pcp_dir: Path, mod_result: dict) -> list[str]:
    """Applies one module's spec_changes/acceptance_changes to disk -- same
    coercion/merge logic the old single-module pm always had, now callable
    per-entry in the modules list. Returns coercion warnings."""
    mod_name = mod_result.get("module_name", "").strip().lower()
    mod_dir = pcp_dir / "strategy" / "modules" / mod_name
    mod_dir.mkdir(parents=True, exist_ok=True)

    spec_path = mod_dir / "spec.yaml"
    acc_path = mod_dir / "acceptance.yaml"

    coercion_warnings: list[str] = []

    # spec_changes is legitimately absent when this pm call only touches
    # acceptance.yaml (e.g. adding/editing criteria with no spec-level
    # change) -- an existing spec.yaml is left untouched in that case,
    # rather than assuming every pm call rewrites the spec.
    if mod_result.get("spec_changes") is not None:
        # Force version 2.0 regardless of what the LLM returned -- same
        # reasoning as kickoff.py: a spec pm touches must always get
        # logic_tier/build_vs_buy enforcement, never silently stay on (or
        # revert to) the ungated 1.0 shape.
        spec_changes = mod_result["spec_changes"]
        spec_changes["version"] = "2.0"

        # On modify, a real prior module-level build_vs_buy decision must not
        # be silently discarded just because this pm call's response omitted
        # it -- only coerce to a flagged placeholder if one never existed.
        existing_spec = {}
        if spec_path.exists():
            try:
                existing_spec = yaml.safe_load(spec_path.read_text()) or {}
            except Exception:
                pass
        if "build_vs_buy" not in spec_changes and existing_spec.get("build_vs_buy"):
            spec_changes["build_vs_buy"] = existing_spec["build_vs_buy"]

        # Same preservation rule for module_logic_breakdown -- the prompt tells
        # the LLM to OMIT this key for a small, same-shape addition (deliberately,
        # to avoid forcing a re-declaration on every trivial pm call), so an
        # omitted key must mean "unchanged", not "delete the prior breakdown".
        if "module_logic_breakdown" not in spec_changes and existing_spec.get("module_logic_breakdown"):
            spec_changes["module_logic_breakdown"] = existing_spec["module_logic_breakdown"]

        coercion_warnings += _normalize_spec(spec_changes, mod_name)
        spec_path.write_text(yaml.dump(spec_changes, default_flow_style=False))

    # Save/Merge acceptance.yaml
    existing_criteria = []
    if acc_path.exists():
        try:
            acc_data = yaml.safe_load(acc_path.read_text()) or {}
            existing_criteria = acc_data.get("criteria", [])
        except Exception:
            pass

    criteria_map = {c["id"]: c for c in existing_criteria}
    for new_c in mod_result.get("acceptance_changes", {}).get("criteria", []):
        # Field-level merge onto the existing entry, not a full replacement --
        # same reasoning as spec_changes's build_vs_buy/module_logic_breakdown
        # preservation above, applied per-criterion. `verified_by` is
        # deliberately excluded from _PM_CRITERION_KEEP_FIELDS (the LLM never
        # sees it, and shouldn't have to), so the LLM's response can NEVER
        # legitimately carry it -- a bare `criteria_map[id] = new_c` replace
        # would silently strip it off any already-`pcp verify`'d criterion pm
        # happens to touch for ANY reason, including an unrelated wording
        # tweak elsewhere in the same module, corrupting the exact
        # complete-but-unverified ambiguity `pcp verify` exists to prevent.
        # Same logic protects evidence/notes/design_justification and any
        # other field pm doesn't manage: preserved unless the response
        # explicitly overwrites it.
        existing_c = criteria_map.get(new_c["id"], {})
        criteria_map[new_c["id"]] = {**existing_c, **new_c}

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

    return coercion_warnings


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

    context_str, dropped = _load_project_context(pcp_dir)
    user_prompt = f"## Intent\n{intent}\n\n{context_str}"

    if dropped:
        console.print(
            f"[dim]Context projection: {dropped:,} chars of existing criteria's "
            f"build_vs_buy/design_justification/QA-evidence fields omitted "
            f"(whole fields by name, nothing truncated mid-value). "
            f"Assembled prompt: {len(user_prompt):,} chars.[/dim]"
        )

    max_context_chars = _max_context_chars()
    if len(user_prompt) > max_context_chars:
        console.print(
            f"[red]Error:[/red] assembled project context is {len(user_prompt):,} chars, "
            f"over the {max_context_chars:,}-char pm limit (~{max_context_chars // 4:,} tokens)."
        )
        console.print(
            "[dim]Already projected down to the fields pm uses and still over the limit -- this "
            "project's spec surface no longer fits in a single context window with room to answer. "
            "Not truncated automatically: a silent cut could drop an unrelated module's constraints "
            "the new intent actually depends on. Either raise PCP_PM_MAX_CONTEXT_CHARS (the model's "
            "real ceiling, not this default, is the binding constraint), or run `pcp pm` against a "
            "narrower project -- a program this size is a candidate for splitting into separate "
            "PCP-managed programs, not just smaller modules.[/dim]"
        )
        sys.exit(2)

    console.print("[dim]Analyzing intent against project context...[/dim]")

    try:
        # Sonnet is the reviewed default for generation calls (see
        # llm/client.py's model-selection strategy) -- replaces the prior
        # ambiguous "inherited/default". PCP_MODEL still overrides.
        result = llm.call_json(SYSTEM_PROMPT, user_prompt, model=llm.BUILD_MODEL, pcp_dir=pcp_dir, command="pm")
    except RuntimeError as e:
        console.print(f"[red]Error calling LLM:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    modules_result = result.get("modules") or []
    if not modules_result:
        console.print("[red]Error: LLM did not identify any module for this intent.[/red]")
        sys.exit(2)

    console.print(f"\n[bold]Intent spans {len(modules_result)} module(s).[/bold]")
    console.print(f"[dim]{result.get('overall_explanation', '')}[/dim]\n")

    for mr in modules_result:
        mod_name = (mr.get("module_name") or "").strip().lower()
        if not mod_name:
            console.print("[red]Error: a module entry is missing module_name.[/red]")
            sys.exit(2)
        console.print(f"[bold]{mr.get('module_action', 'modify').upper()} module[/bold] [cyan]'{mod_name}'[/cyan]")
        console.print(f"[dim]{mr.get('module_explanation', '')}[/dim]")
        if mr.get("spec_changes"):
            console.print("[bold]Proposed spec.yaml changes:[/bold]")
            console.print(yaml.dump(mr["spec_changes"], default_flow_style=False))
        else:
            console.print("[bold]Proposed spec.yaml changes:[/bold] [dim](none -- acceptance.yaml only)[/dim]")
        console.print("[bold]Proposed acceptance.yaml criteria to add:[/bold]")
        for c in mr.get("acceptance_changes", {}).get("criteria", []):
            console.print(f"  - [{c['id']}] {c['description']} (check: {c.get('check', 'manual')})")
        console.print("")

    if not click.confirm(f"Approve these changes across {len(modules_result)} module(s) and queue them for build?"):
        console.print("[yellow]Changes aborted.[/yellow]")
        sys.exit(0)

    coercion_warnings: list[str] = []
    for mr in modules_result:
        coercion_warnings += _write_one_module(pcp_dir, mr)

    console.print(f"[green]✓[/green] {len(modules_result)} module(s) updated.")
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

    # Deterministic, zero-cost capability coverage cross-check (see
    # DECOMPOSE FIRST in SYSTEM_PROMPT) -- runs before the LLM-judged
    # validate-strategy call, not instead of it.
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    decomposition_path = pcp_dir / "strategy" / "decomposition.md"
    decomposition = decomposition_path.read_text() if decomposition_path.exists() else ""
    all_specs = {}
    for spec_path in sorted((pcp_dir / "strategy" / "modules").glob("*/spec.yaml")):
        try:
            all_specs[spec_path.parent.name] = yaml.safe_load(spec_path.read_text()) or {}
        except Exception:
            pass

    capability_warnings = check_capability_coverage(result.get("capabilities_enumerated", []), all_specs)
    if capability_warnings:
        console.print(f"[yellow]⚠  {len(capability_warnings)} enumerated capability(ies) may not be covered by any module:[/yellow]")
        for w in capability_warnings:
            console.print(f"   {w}")
        console.print(
            "   [dim]For any of these, `pcp inspiration-art --gap \"<capability>\"` proposes a "
            "researched category to cover it, instead of leaving the gap silent.[/dim]"
        )

    # Same check, one layer deeper -- any module whose spec now declares
    # module_logic_breakdown gets it cross-checked against its OWN criteria.
    all_acceptances = {}
    for acc_path in sorted((pcp_dir / "strategy" / "modules").glob("*/acceptance.yaml")):
        try:
            all_acceptances[acc_path.parent.name] = yaml.safe_load(acc_path.read_text()) or {}
        except Exception:
            pass
    breakdown_warnings = check_module_logic_breakdown_coverage(all_specs, all_acceptances)
    if breakdown_warnings:
        console.print(f"[yellow]⚠  {len(breakdown_warnings)} logic-breakdown item(s) may not be covered by their own module's criteria:[/yellow]")
        for w in breakdown_warnings:
            console.print(f"   {w}")

    # Sharper coverage signal than the module-level check above -- a
    # capability can textually match a module's broad objective_coverage
    # while no actual CRITERION implements it. See check_capability_
    # criterion_coverage's docstring (kickoff.py) for the real incident this closes.
    criterion_capability_warnings = check_capability_criterion_coverage(result.get("capabilities_enumerated", []), all_acceptances)
    if criterion_capability_warnings:
        console.print(f"[yellow]⚠  {len(criterion_capability_warnings)} enumerated capability(ies) matched a module but no actual criterion implements them:[/yellow]")
        for w in criterion_capability_warnings:
            console.print(f"   {w}")

    # Prior-art evidence cross-check -- see check_prior_art_evidence's
    # docstring in kickoff.py. Same rationale as kickoff's own call: pm can
    # add/modify a module's build_vs_buy just as easily as kickoff can.
    priorart_warnings = check_prior_art_evidence(all_specs)
    if priorart_warnings:
        console.print(f"[yellow]⚠  {len(priorart_warnings)} module(s) may be missing prior-art search evidence:[/yellow]")
        for w in priorart_warnings:
            console.print(f"   {w}")

    # Run validate-strategy automatically -- pm previously had ZERO strategy
    # verification at all (unlike kickoff, which always called this), the
    # real root cause of "pm sometimes misses components" -- a module gets
    # modified/added in isolation with nothing checking whether the project
    # still covers the objective afterward.
    if objective:
        console.print("\n[bold]Running validate-strategy...[/bold]")
        val_user_prompt = build_val_prompt(objective, decomposition, all_specs)
        try:
            val_result = llm.call_json(
                VAL_SYSTEM_PROMPT, val_user_prompt,
                model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="pm-validate",
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Could not run validate-strategy automatically: {e}[/yellow]")
            val_result = None
        if val_result:
            render_val_results(pcp_dir, val_result, output_json=False)

    _warn_stale_decomposition(pcp_dir, decomposition, modules_result, intent)

    console.print("[green]✓[/green] Project state refreshed. Run [cyan]pcp build[/cyan] to begin development.")


def _decomposition_is_stale(decomposition: str, modules_result: list[dict]) -> list[str]:
    """Module names this pm call touched that decomposition.md never mentions.

    Deterministic substring check (rung 1) — no LLM deciding whether the
    strategy doc is still true. Adding a module via `pcp pm` silently left
    decomposition.md describing a module set that no longer exists, and since
    validate-strategy judges specs against that same stale decomposition, the
    gap could persist indefinitely."""
    if not decomposition.strip():
        return []
    haystack = decomposition.lower()
    stale = []
    for mr in modules_result:
        name = (mr.get("module_name") or "").strip().lower()
        if not name:
            continue
        if name not in haystack and name.replace("_", "-") not in haystack and name.replace("_", " ") not in haystack:
            stale.append(name)
    return stale


def _warn_stale_decomposition(pcp_dir, decomposition: str, modules_result: list[dict], intent: str) -> None:
    """pm deliberately never writes program-level spec files (that separation is
    why correct-objective exists as its own command), so this surfaces the
    drift and offers to run the human-approved `pcp amend` path right here
    rather than leaving a nudge nobody acts on."""
    stale = _decomposition_is_stale(decomposition, modules_result)
    if not stale:
        return
    console.print(
        f"\n[yellow]⚠  decomposition.md does not mention {', '.join(stale)} — "
        "the strategy doc validate-strategy judges against is now stale.[/yellow]"
    )
    change = f"Module(s) {', '.join(stale)} were added/changed via `pcp pm`: {intent}"
    try:
        wants_fix = click.confirm("Amend decomposition.md now (you'll review the diff)?", default=True)
    except (click.Abort, RuntimeError, EOFError):
        wants_fix = False
    if not wants_fix:
        console.print(f'[dim]Run later: pcp amend decomposition "{change}"[/dim]')
        return
    from pcp.commands.amend import amend
    ctx = click.get_current_context(silent=True)
    if ctx:
        ctx.invoke(amend, target_file="decomposition", change=change,
                   project_path=str(pcp_dir.parent), yes=False, allow_weakening=False)
    else:
        console.print(f'[dim]Run: pcp amend decomposition "{change}"[/dim]')
