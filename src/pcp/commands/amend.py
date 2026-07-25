"""pcp amend — the human-authorized write path for every protected `.pcp/`
file that previously had none.

Before this, of the 10 paths in ci_rules.yaml's `protected_path` scope only 4
had a propose/approve/write command: objective.md + target_state.md via
`pcp correct-objective`, and modules/*/spec.yaml + acceptance.yaml via
`pcp kickoff`/`pcp pm`. The other six -- architecture.md, decomposition.md,
dependency_map.md, ci_rules.yaml, controls.yaml, SDLC_phase.yaml -- were
written once at kickoff (or, for dependency_map.md, only by `pcp import`) and
had no path afterwards. Combined with doctrine wording that told agents never
to edit a protected file, the practical outcome was that a detailed feature
discussion ending in "now update the specs" updated nothing at all.

`amend` closes that. Same mechanic as `correct-objective` (see spec_write.py):
LLM proposes from stated intent, human reviews a real unified diff, approves,
then it's written. The three governance files additionally get schema
validation and a deterministic weakening check before the human is even asked.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp import spec_write
from pcp.llm import client as llm
from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.spec_write import SpecTarget

console = Console()

# Files whose amendment changes what modules exist / what they cover, and so
# must be re-checked against the objective immediately afterwards.
_REVALIDATES = {"decomposition", "dependency_map"}


def _targets(pcp_dir: Path) -> dict[str, SpecTarget]:
    return {
        "architecture": SpecTarget(
            name="architecture.md",
            path=pcp_dir / "architecture.md",
            key="content",
            description="program-level tech decisions and constraints",
        ),
        "decomposition": SpecTarget(
            name="strategy/decomposition.md",
            path=pcp_dir / "strategy" / "decomposition.md",
            key="content",
            description="how the objective breaks into modules, and why",
        ),
        "dependency_map": SpecTarget(
            name="strategy/dependency_map.md",
            path=pcp_dir / "strategy" / "dependency_map.md",
            key="content",
            description="module build order and inter-module contracts",
        ),
        "ci_rules": SpecTarget(
            name="ci_rules.yaml",
            path=pcp_dir / "ci_rules.yaml",
            key="content",
            schema="ci_rules",
            guarded=True,
            description="the program's enforced laws (Layer 1 gates)",
        ),
        "controls": SpecTarget(
            name="controls.yaml",
            path=pcp_dir / "controls.yaml",
            key="content",
            schema="controls",
            guarded=True,
            description="the control catalog, cross-referenced to SSDF practices",
        ),
        "sdlc_phase": SpecTarget(
            name="SDLC_phase.yaml",
            path=pcp_dir / "SDLC_phase.yaml",
            key="content",
            schema="sdlc_phase",
            guarded=True,
            description="current SDLC phase and its machine-enforced exit criteria",
        ),
    }

# Accepted aliases, so `pcp amend architecture.md` and `pcp amend
# strategy/decomposition.md` work as well as the short keys.
_ALIASES = {
    "architecture.md": "architecture",
    "decomposition.md": "decomposition",
    "strategy/decomposition.md": "decomposition",
    "dependency-map": "dependency_map",
    "dependency_map.md": "dependency_map",
    "strategy/dependency_map.md": "dependency_map",
    "ci-rules": "ci_rules",
    "ci_rules.yaml": "ci_rules",
    "controls.yaml": "controls",
    "sdlc": "sdlc_phase",
    "sdlc-phase": "sdlc_phase",
    "sdlc_phase.yaml": "sdlc_phase",
    "SDLC_phase.yaml": "sdlc_phase",
}


def resolve_target_key(raw: str) -> str | None:
    """Map a user-typed file argument onto a registry key. Deterministic
    lookup, never fuzzy — an LLM guessing which protected file the human meant
    is exactly the drift this command exists to prevent."""
    if raw in _ALIASES:
        return _ALIASES[raw]
    # Order matters: strip "./" before ".pcp/", and never lstrip(chars) here --
    # lstrip("./") eats the leading dot of ".pcp/" itself.
    normalised = raw.strip().removeprefix("./").removeprefix(".pcp/").removeprefix("/")
    if normalised in _ALIASES:
        return _ALIASES[normalised]
    key = normalised.replace("-", "_")
    return key if key in {
        "architecture", "decomposition", "dependency_map",
        "ci_rules", "controls", "sdlc_phase",
    } else None


_MARKDOWN_RULES = """\
- Keep everything NOT affected by the change exactly as-is -- do not rephrase, \
reorder, or "improve" unrelated sections.
- Change only what the stated change actually requires.
- Do not invent scope beyond what the change states.
- Preserve the file's existing structure and heading style."""

_YAML_RULES = """\
- Keep everything NOT affected by the change exactly as-is, including comments, \
key order, and formatting.
- Change only what the stated change actually requires.
- NEVER remove an existing rule/control/exit-criterion, and never downgrade a \
hard_block severity, unless the stated change explicitly and unambiguously asks \
for that removal or downgrade.
- Preserve every existing id verbatim -- ids are written into telemetry.jsonl by \
gate call sites and renaming one silently breaks the audit trail."""


def build_system_prompt(target: SpecTarget) -> str:
    is_yaml = target.name.endswith(".yaml")
    rules = _YAML_RULES if is_yaml else _MARKDOWN_RULES
    return (
        f"You are the program's spec author. A human has decided, during "
        f"conversation, on a change that must now be reflected in "
        f"`.pcp/{target.name}` ({target.description}). You are given the "
        f"current file in full, plus read-only context.\n\n"
        f"Rewrite the file to incorporate the change faithfully:\n{rules}\n\n"
        f"You must output ONLY valid JSON — no prose, no markdown, no code "
        f"fences.\n\nOutput schema:\n{spec_write.build_output_schema([target])}\n"
    )


def build_user_prompt(pcp_dir: Path, target: SpecTarget, change: str) -> str:
    sections = [f"## Requested change\n{change}"]

    current = target.path.read_text() if target.path.exists() else "(file does not exist yet)"
    sections.append(f"## Current {target.name}\n{current}")

    # Read-only context — never rewritten by this command, only used so the
    # proposal stays consistent with what the program already says.
    objective = pcp_dir / "objective.md"
    if objective.exists():
        sections.append(f"## Read-only context: objective.md\n{objective.read_text()}")

    decomposition = pcp_dir / "strategy" / "decomposition.md"
    if target.name != "strategy/decomposition.md" and decomposition.exists():
        sections.append(f"## Read-only context: decomposition.md\n{decomposition.read_text()}")

    modules_dir = pcp_dir / "strategy" / "modules"
    if modules_dir.exists():
        summaries = []
        for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
            try:
                spec = yaml.safe_load(spec_path.read_text()) or {}
            except yaml.YAMLError:
                continue
            name = spec.get("module") or spec_path.parent.name
            purpose = str(spec.get("purpose", "")).strip().splitlines()[:1]
            depends = spec.get("depends_on") or spec.get("dependencies") or []
            summaries.append(
                f"- {name}: {purpose[0] if purpose else ''} (depends_on: {depends or 'none'})"
            )
        if summaries:
            sections.append("## Read-only context: existing modules\n" + "\n".join(summaries))

    return "\n\n".join(sections)


def _revalidate(pcp_dir: Path) -> None:
    """Re-run validate-strategy after a change to what modules exist/cover —
    same posture as correct-objective's. Advisory: a failure here never undoes
    the write the human already approved."""
    from pcp.commands.validate_strategy import (
        _build_user_prompt as build_val_prompt,
        SYSTEM_PROMPT as VAL_SYSTEM_PROMPT,
        _render_results as render_val_results,
    )

    console.print("\n[bold]Running validate-strategy against the amended strategy...[/bold]")
    obj_path = pcp_dir / "objective.md"
    objective = obj_path.read_text() if obj_path.exists() else ""
    dec_path = pcp_dir / "strategy" / "decomposition.md"
    decomposition = dec_path.read_text() if dec_path.exists() else ""
    all_specs = {}
    for spec_path in sorted((pcp_dir / "strategy" / "modules").glob("*/spec.yaml")):
        try:
            all_specs[spec_path.parent.name] = yaml.safe_load(spec_path.read_text()) or {}
        except Exception:
            pass
    try:
        val_prompt = build_val_prompt(objective, decomposition, all_specs)
        val_result = llm.call_json(
            VAL_SYSTEM_PROMPT, val_prompt, model=llm.JUDGE_MODEL,
            pcp_dir=pcp_dir, command="amend-validate",
        )
        render_val_results(pcp_dir, val_result, output_json=False)
    except Exception as e:
        console.print(f"[yellow]Warning: could not run validate-strategy automatically: {e}[/yellow]")


@click.command("amend")
@click.argument("target_file")
@click.argument("change")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--yes", "yes", is_flag=True, help="Skip the interactive diff-approval prompt (scripted/CI use).")
@click.option("--allow-weakening", "allow_weakening", is_flag=True,
              help="Permit a governance-file rewrite that removes a rule/control/exit-criterion or downgrades a hard_block. Recorded in decision_log.jsonl.")
def amend(target_file: str, change: str, project_path: str | None, yes: bool, allow_weakening: bool):
    """Propose + human-approve a rewrite of a human-authorized .pcp/ file.

    TARGET_FILE is one of: architecture, decomposition, dependency_map,
    ci_rules, controls, sdlc_phase (file names like `architecture.md` also work).

    For objective.md/target_state.md use `pcp correct-objective`; for module
    spec.yaml/acceptance.yaml use `pcp pm`.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    key = resolve_target_key(target_file)
    if key is None:
        if target_file.strip().removeprefix("./").removeprefix(".pcp/") in (
            "objective.md", "target_state.md", "objective", "target_state"
        ):
            console.print("[red]Error:[/red] use `pcp correct-objective \"<correction>\"` for objective.md/target_state.md.")
            sys.exit(2)
        if "spec.yaml" in target_file or "acceptance.yaml" in target_file:
            console.print("[red]Error:[/red] use `pcp pm \"<intent>\"` for module spec.yaml/acceptance.yaml.")
            sys.exit(2)
        console.print(
            f"[red]Error:[/red] {target_file} is not an amendable file. Choose one of: "
            "architecture, decomposition, dependency_map, ci_rules, controls, sdlc_phase."
        )
        sys.exit(2)

    target = _targets(pcp_dir)[key]
    if not change.strip():
        console.print("[red]Error:[/red] pass the change to make as the second argument.")
        sys.exit(2)

    outcome = spec_write.propose_and_write(
        pcp_dir,
        [target],
        build_system_prompt(target),
        build_user_prompt(pcp_dir, target, change),
        command="amend",
        intent=change,
        yes=yes,
        allow_weakening=allow_weakening,
        no_change_hint="If this is unexpected, restate the change more concretely.",
    )
    if not outcome.written:
        sys.exit(0)

    if key in _REVALIDATES:
        _revalidate(pcp_dir)
    elif target.guarded:
        console.print("\n[dim]Gate definitions changed — run `pcp check` before your next commit "
                      "so Layer 1 runs against the amended rules.[/dim]")
