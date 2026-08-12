"""Shared propose -> real-diff -> human-approve -> write mechanic for every
human-authorized `.pcp/` file.

Hard Rule #2 is "human-AUTHORIZED only", not "human-typed only". The
`protected_path` ci_rule it rides on has always meant exactly that -- read
`check.run_protected_path_rule`: it returns zero violations unless
`PCP_AGENT_SESSION=1`, i.e. it only ever blocks `pcp build`'s unattended
coding agent. A human-present session was never blocked by code.

What DID block it was doctrine wording. "Spec files are human-written only /
never edit these as an agent" (scaffolded into every PCP project's CLAUDE.md
by `pcp init`) told agents to refuse, and `pcp init`'s own escape hatch --
"propose changes via `pcp pm`" -- was a dead end for 6 of the 10 protected
paths, because `pm` only ever writes module spec.yaml/acceptance.yaml. So a
detailed feature discussion would end with "update the specs", the agent
would decline on doctrine, and nothing got written. Reported by Ganesh
2026-07-25 as a recurring, cross-project failure.

This module is the generalisation of what `pcp correct-objective` already did
for objective.md/target_state.md: an LLM proposes the rewrite from stated
intent, the human sees a real unified diff of every affected file, approves,
and only then is anything written. Every human-authorized file now has such a
path (`pcp amend`), so "human-authorized" is an achievable instruction rather
than a refusal.

Guardrail for the governance files (ci_rules.yaml, controls.yaml,
SDLC_phase.yaml): an LLM proposing edits to the rules that police it is a
genuine self-reference risk. Those three are schema-validated before write,
and any proposal that deletes a rule/control or downgrades a hard_block is
refused unless the human passes --allow-weakening (and it is recorded either
way). Approval alone is not enough there -- weakening must be deliberate and
named.
"""

import difflib
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp import decision_log, protected_writes
from pcp.llm import client as llm
from pcp.schema import validator

console = Console()


@dataclass
class SpecTarget:
    """One human-authorized file participating in a propose/approve write."""

    name: str  # display + diff label, e.g. "strategy/decomposition.md"
    path: Path
    key: str  # JSON key the LLM returns the full replacement content under
    schema: str | None = None  # schema name for validator.validate_file, if any
    guarded: bool = False  # True => weakening check applies (governance files)
    description: str = ""


@dataclass
class WriteResult:
    written: bool
    result: dict = field(default_factory=dict)
    reason: str = ""


def render_diff(old: str, new: str, name: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
            lineterm="",
        )
    )


def build_output_schema(targets: list[SpecTarget]) -> str:
    """The JSON output contract to append to a caller's system prompt, so every
    amend prompt describes its return shape identically."""
    lines = ["{"]
    for t in targets:
        lines.append(f'  "{t.key}": "full replacement content of {t.name}",')
    lines.append('  "summary": "one paragraph: exactly what changed and why, for the audit trail"')
    lines.append("}")
    return "\n".join(lines)


def _schema_errors(text: str, schema_name: str) -> list[str]:
    """Validate proposed content without writing it to its real path first."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        return validator.validate_file(tmp, schema_name)
    finally:
        tmp.unlink(missing_ok=True)


HARD = "hard_block"
_SEVERITY_RANK = {"advisory": 0, "warn": 1, "warning": 1, HARD: 2}


def detect_weakening(old_text: str, new_text: str) -> list[str]:
    """Governance-file weakening detector: removed rules/controls, or a
    severity downgraded away from hard_block. Deterministic (rung 1) -- never
    an LLM judging whether its own proposed edit weakened the gate.

    Unparseable-old is treated as no findings (nothing trustworthy to compare);
    unparseable-new is caught by the schema check, not here."""
    try:
        old = yaml.safe_load(old_text) or {}
        new = yaml.safe_load(new_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(old, dict) or not isinstance(new, dict):
        return []

    findings = []
    for collection in ("rules", "controls"):
        old_items = {i.get("id"): i for i in (old.get(collection) or []) if isinstance(i, dict) and i.get("id")}
        new_items = {i.get("id"): i for i in (new.get(collection) or []) if isinstance(i, dict) and i.get("id")}
        for rid in sorted(set(old_items) - set(new_items)):
            findings.append(f"{collection}: {rid} removed")
        for rid in sorted(set(old_items) & set(new_items)):
            old_sev = str(old_items[rid].get("severity", "")).lower()
            new_sev = str(new_items[rid].get("severity", "")).lower()
            if old_sev == new_sev:
                continue
            if _SEVERITY_RANK.get(new_sev, 0) < _SEVERITY_RANK.get(old_sev, 0):
                findings.append(f"{collection}: {rid} severity {old_sev} -> {new_sev} (downgrade)")

    # SDLC_phase.yaml has no rules/controls list — dropped phases, or dropped
    # exit criteria within a surviving phase, are the equivalent weakening.
    old_phases = {p.get("name"): p for p in (old.get("phases") or []) if isinstance(p, dict) and p.get("name")}
    new_phases = {p.get("name"): p for p in (new.get("phases") or []) if isinstance(p, dict) and p.get("name")}
    for name in sorted(set(old_phases) - set(new_phases)):
        findings.append(f"phases: {name} removed")
    for name in sorted(set(old_phases) & set(new_phases)):
        old_ids = {c.get("id") for c in (old_phases[name].get("exit_criteria") or []) if isinstance(c, dict)}
        new_ids = {c.get("id") for c in (new_phases[name].get("exit_criteria") or []) if isinstance(c, dict)}
        for cid in sorted(old_ids - new_ids):
            findings.append(f"phases.{name}.exit_criteria: {cid} removed")

    return findings


def propose_and_write(
    pcp_dir: Path,
    targets: list[SpecTarget],
    system_prompt: str,
    user_prompt: str,
    *,
    command: str,
    intent: str,
    model: str | None = None,
    yes: bool = False,
    allow_weakening: bool = False,
    decision_category: str = "architecture",
    no_change_hint: str = "",
) -> WriteResult:
    """Draft a rewrite of every target, show a real diff, require approval, write.

    Exits(2) on LLM error, a missing key in the response, a schema violation, or
    unapproved weakening of a guarded file. Returns WriteResult(written=False)
    for the benign outcomes (nothing changed, human declined) so callers can
    keep their own exit code at 0.
    """
    old_texts = {t.key: (t.path.read_text() if t.path.exists() else "") for t in targets}

    console.print(f"[dim]Drafting {', '.join(t.name for t in targets)} rewrite...[/dim]")
    try:
        result = llm.call_json(
            system_prompt,
            user_prompt,
            model=model or llm.BUILD_MODEL,
            pcp_dir=pcp_dir,
            command=command,
        )
    except (RuntimeError, ValueError) as e:
        console.print(f"[red]Error calling LLM:[/red] {e}")
        sys.exit(2)

    new_texts = {}
    for t in targets:
        content = result.get(t.key, "")
        if not content:
            console.print(f"[red]Error:[/red] LLM did not return content for {t.name}.")
            sys.exit(2)
        new_texts[t.key] = content

    diffs = {t.name: render_diff(old_texts[t.key], new_texts[t.key], t.name) for t in targets}
    if not any(diffs.values()):
        console.print(
            "[yellow]No changes -- the stated change doesn't appear to require a rewrite. "
            f"{no_change_hint or 'If this is unexpected, restate it more concretely.'}[/yellow]"
        )
        return WriteResult(written=False, result=result, reason="no-change")

    # Schema + weakening checks run BEFORE the human is asked to approve, so an
    # invalid or gate-weakening proposal never even reaches the prompt.
    for t in targets:
        if not diffs[t.name]:
            continue
        if t.schema:
            errors = _schema_errors(new_texts[t.key], t.schema)
            if errors:
                console.print(f"[red]Error:[/red] proposed {t.name} fails its schema — not written.")
                for err in errors[:10]:
                    console.print(f"   {err}")
                sys.exit(2)
        if t.guarded:
            weakened = detect_weakening(old_texts[t.key], new_texts[t.key])
            if weakened and not allow_weakening:
                console.print(
                    f"[red]Refused:[/red] proposed {t.name} weakens the gates that police this project:"
                )
                for w in weakened:
                    console.print(f"   {w}")
                console.print(
                    "\n[dim]Approval alone is not enough for a governance file. Re-run with "
                    "--allow-weakening if this is deliberate — it is recorded in decision_log.jsonl.[/dim]"
                )
                sys.exit(2)

    console.print(f"\n[bold]Summary:[/bold] {result.get('summary', '')}\n")
    for t in targets:
        if diffs[t.name]:
            console.print(f"[bold]{t.name} diff:[/bold]")
            console.print(diffs[t.name])
            console.print("")

    changed = [t.name for t in targets if diffs[t.name]]
    if not yes and not click.confirm(f"Apply this rewrite to {', '.join(changed)}?"):
        console.print("[yellow]Aborted -- files unchanged.[/yellow]")
        return WriteResult(written=False, result=result, reason="declined")

    weakenings = []
    for t in targets:
        if not diffs[t.name]:
            continue
        if t.guarded:
            weakenings += [f"{t.name}: {w}" for w in detect_weakening(old_texts[t.key], new_texts[t.key])]
        t.path.parent.mkdir(parents=True, exist_ok=True)
        t.path.write_text(new_texts[t.key])
        protected_writes.record_approved_write(pcp_dir, t.path, new_texts[t.key])
    console.print(f"[green]✓[/green] {', '.join(changed)} rewritten.")

    evidence = result.get("summary", "")
    if weakenings:
        console.print("[yellow]⚠  gate weakening applied with --allow-weakening:[/yellow]")
        for w in weakenings:
            console.print(f"   {w}")
        evidence = f"{evidence}\n\nGATE WEAKENING (--allow-weakening): " + "; ".join(weakenings)

    decision_log.record(
        pcp_dir,
        source=command,
        session_id=None,
        category=decision_category,
        summary=f"{', '.join(changed)} amended: {intent}",
        evidence=evidence,
    )

    return WriteResult(written=True, result=result)
