"""pcp correct-objective — the human-authorized write path for objective.md/
target_state.md.

Hard Rule #2 ("spec files are human-authorized only") protects against an
autonomous build-agent silently drifting the spec (`protected_path` hard-blocks
any write while `PCP_AGENT_SESSION=1`). It was never meant to mean "Ganesh
hand-types the markdown diff himself" -- `pcp kickoff`/`pcp pm` already write
module-level spec.yaml/acceptance.yaml this same way: LLM proposes the change
from stated intent, a human reviews and approves, then it's written. objective.md
and target_state.md never got the equivalent command, so in practice a business
correction discussed and agreed in conversation had nowhere to go except a
human manually opening the file -- and the 2026-07-22 Project O incident
is exactly what happens when that manual step is skipped and nothing catches
it: a build cycle runs to completion against a stale objective.

Deliberately separate from `pcp pm`: pm never touches the program-level spec
files, only module specs -- keeping this a distinct, explicit command means
running it always shows a real diff of the two most consequential files in
the project, never gets silently bundled into a routine module-intent call.

The propose/diff/approve/write mechanic itself now lives in `spec_write.py`,
shared with `pcp amend` — which gives the same treatment to the six other
protected files that had no write path at all (2026-07-25).
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.llm import client as llm
from pcp import objective_conflicts
from pcp import spec_write
from pcp.spec_write import SpecTarget
from pcp.commands.validate_strategy import (
    _build_user_prompt as build_val_prompt,
    SYSTEM_PROMPT as VAL_SYSTEM_PROMPT,
    _render_results as render_val_results,
)

console = Console()

SYSTEM_PROMPT = """\
You are the program's spec author. A human has communicated a business \
correction, during conversation, that must now be reflected in the immutable \
program objective. You are given the CURRENT objective.md and target_state.md \
in full.

Rewrite them to incorporate the correction faithfully:
- Keep everything NOT affected by the correction exactly as-is -- do not \
rephrase, reorder, or "improve" unrelated sections.
- Change only what the correction actually requires.
- Do not invent new scope beyond what the correction states.
- If the correction only affects one of the two files, still return both \
(the untouched one unchanged).

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "objective_md": "full replacement content of objective.md",
  "target_state_md": "full replacement content of target_state.md",
  "summary": "one paragraph: exactly what changed and why, for the audit trail"
}
"""


def _load_conflict(pcp_dir: Path, item_id: str) -> dict | None:
    path = pcp_dir / "brd_items.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    for item in data.get("items", []):
        if item.get("id") == item_id:
            return item
    return None


@click.command("correct-objective")
@click.argument("correction", required=False, default=None)
@click.option("--from-conflict", "from_conflict", default=None, metavar="ITEM_ID",
              help="Pull the correction text from an unresolved brd_items.yaml conflict (see `pcp objective-conflicts`) instead of a positional argument.")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--yes", "yes", is_flag=True, help="Skip the interactive diff-approval prompt (scripted/CI use).")
def correct_objective(correction: str | None, from_conflict: str | None, project_path: str | None, yes: bool):
    """Propose + human-approve a rewrite of objective.md/target_state.md from a stated business correction."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    conflict_item = None
    if from_conflict:
        conflict_item = _load_conflict(pcp_dir, from_conflict)
        if not conflict_item:
            console.print(f"[red]Error:[/red] no brd_items.yaml entry with id {from_conflict}")
            sys.exit(2)
        correction = f"{conflict_item.get('description', '')} -- conflict noted: {conflict_item.get('drift_flag', '')}"
    elif not correction:
        console.print("[red]Error:[/red] pass a correction as an argument, or --from-conflict ITEM_ID")
        sys.exit(2)

    obj_path = pcp_dir / "objective.md"
    ts_path = pcp_dir / "target_state.md"

    user_prompt = "\n\n".join([
        f"## Correction\n{correction}",
        f"## Current objective.md\n{obj_path.read_text() if obj_path.exists() else ''}",
        f"## Current target_state.md\n{ts_path.read_text() if ts_path.exists() else ''}",
    ])

    outcome = spec_write.propose_and_write(
        pcp_dir,
        [
            SpecTarget(name="objective.md", path=obj_path, key="objective_md"),
            SpecTarget(name="target_state.md", path=ts_path, key="target_state_md"),
        ],
        SYSTEM_PROMPT,
        user_prompt,
        command="correct-objective",
        intent=correction,
        yes=yes,
        no_change_hint="If this is unexpected, restate the correction more concretely.",
    )
    if not outcome.written:
        sys.exit(0)
    new_objective = outcome.result["objective_md"]

    # Auto-clears any objective_conflicts entry whose flagged hash no longer
    # matches — including conflict_item itself, since the file just changed.
    still_unresolved = objective_conflicts.reconcile(pcp_dir)
    if conflict_item and not any(c.get("id") == conflict_item.get("id") for c in still_unresolved):
        console.print(f"[green]✓[/green] conflict {conflict_item['id']} resolved.")

    # Objective just moved — every module's spec should be re-checked against
    # it before anyone runs `pcp build`, same as kickoff/pm always do.
    console.print("\n[bold]Running validate-strategy against the new objective...[/bold]")
    decomposition_path = pcp_dir / "strategy" / "decomposition.md"
    decomposition = decomposition_path.read_text() if decomposition_path.exists() else ""
    all_specs = {}
    for spec_path in sorted((pcp_dir / "strategy" / "modules").glob("*/spec.yaml")):
        try:
            all_specs[spec_path.parent.name] = yaml.safe_load(spec_path.read_text()) or {}
        except Exception:
            pass
    try:
        val_prompt = build_val_prompt(new_objective, decomposition, all_specs)
        val_result = llm.call_json(VAL_SYSTEM_PROMPT, val_prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="correct-objective-validate")
        render_val_results(pcp_dir, val_result, output_json=False)
    except Exception as e:
        console.print(f"[yellow]Warning: could not run validate-strategy automatically: {e}[/yellow]")

    console.print(
        "\n[dim]Existing module specs may now be stale against the new objective (see validate-strategy "
        "output above) -- run `pcp pm \"...\"` per affected module before `pcp build`.[/dim]"
    )
