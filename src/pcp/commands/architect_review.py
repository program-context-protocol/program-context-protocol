"""pcp architect-review — architecture principle review against persona + KB."""

import json
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.llm import client as llm

console = Console()

SYSTEM_PROMPT = """\
You are a senior software architect reviewing code changes.
You have a project-specific persona, architecture constraints, and a curated knowledge base.
Review the provided diff (or module spec) against the persona rules and KB.

Output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "findings": [
    {
      "severity": "BLOCK | WARN | NOTE",
      "location": "file:line or module name or 'general'",
      "principle": "which architecture principle or persona rule was violated",
      "finding": "what is wrong",
      "fix": "concrete suggestion to fix it"
    }
  ],
  "summary": "one sentence overall assessment",
  "blocks": 0,
  "warns": 0
}

severity:
  BLOCK = must fix before merge (violates hard architecture constraint)
  WARN  = fix before ship (design smell, weak boundary, missing invariant)
  NOTE  = track, non-blocking (observation for future consideration)

If no findings, return {"findings": [], "summary": "No architecture violations found.", "blocks": 0, "warns": 0}
"""


def _get_diff(base: str) -> str:
    result = subprocess.run(
        ["git", "diff", f"{base}...HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout[:14000]


def _get_staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True, text=True,
    )
    return result.stdout[:14000]


def _load_persona(pcp_dir: Path) -> str:
    persona_path = pcp_dir / "architect_persona.md"
    if not persona_path.exists():
        return ""
    return persona_path.read_text()


def _load_kb(pcp_dir: Path, changed_files: list[str]) -> str:
    """Load ADRs always. Load domain KB contextually based on changed files."""
    kb_dir = pcp_dir / "kb"
    if not kb_dir.exists():
        return ""

    parts = []

    # ADRs — always load (small, high signal)
    adr_dir = kb_dir / "adr"
    if adr_dir.exists():
        adr_files = sorted(adr_dir.glob("*.md"))
        if adr_files:
            parts.append("## Architecture Decision Records\n")
            for f in adr_files:
                parts.append(f"### {f.stem}\n{f.read_text()}\n")

    # Domain KB — contextual
    domain_dir = kb_dir / "domain"
    if domain_dir.exists():
        for domain_file in sorted(domain_dir.glob("*.md")):
            tag = domain_file.stem.lower()
            # Load if any changed file path contains the tag word
            if any(tag in cf.lower() for cf in changed_files) or not changed_files:
                parts.append(f"## Domain KB: {domain_file.stem}\n{domain_file.read_text()}\n")

    return "\n".join(parts)


def _changed_files_from_diff(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


def _load_module_spec(pcp_dir: Path, module_name: str) -> str:
    mod_dir = pcp_dir / "strategy" / "modules" / module_name
    parts = []
    spec = mod_dir / "spec.yaml"
    acceptance = mod_dir / "acceptance.yaml"
    if spec.exists():
        parts.append(f"## Module Spec ({module_name})\n```yaml\n{spec.read_text()}\n```")
    if acceptance.exists():
        parts.append(f"## Acceptance Criteria\n```yaml\n{acceptance.read_text()}\n```")
    return "\n\n".join(parts)


def _build_prompt(
    persona: str,
    architecture: str,
    kb: str,
    diff_or_spec: str,
    mode: str,
) -> str:
    parts = []

    if persona:
        parts.append(f"## Architect Persona\n\n{persona}")

    if architecture:
        parts.append(f"## Project Architecture Constraints\n\n{architecture}")

    if kb:
        parts.append(f"## Knowledge Base\n\n{kb}")

    if mode == "diff":
        parts.append(f"## Code Diff to Review\n\n```diff\n{diff_or_spec}\n```")
        parts.append("Review this diff against your persona rules, architecture constraints, and KB.")
    else:
        parts.append(f"## Module Spec to Review\n\n{diff_or_spec}")
        parts.append("Review this module spec for architecture principle violations.")

    return "\n\n".join(parts)


def _render_table(result: dict) -> None:
    findings = result.get("findings", [])
    blocks = result.get("blocks", 0)
    warns = result.get("warns", 0)

    summary = result.get("summary", "")
    color = "green" if blocks == 0 and warns == 0 else ("red" if blocks > 0 else "yellow")
    console.print(f"\n[bold]Architecture Review[/bold]  [{color}]{blocks} BLOCK  {warns} WARN[/{color}]")
    console.print(f"[dim]{summary}[/dim]\n")

    if not findings:
        console.print("  [green]✓[/green]  No architecture violations found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Sev", width=6)
    table.add_column("Location", width=28)
    table.add_column("Principle", width=22)
    table.add_column("Finding")

    sev_colors = {"BLOCK": "red", "WARN": "yellow", "NOTE": "dim"}

    for f in findings:
        sev = f.get("severity", "NOTE")
        color = sev_colors.get(sev, "dim")
        table.add_row(
            f"[{color}]{sev}[/{color}]",
            f.get("location", ""),
            f.get("principle", ""),
            f"{f.get('finding', '')}  →  [italic]{f.get('fix', '')}[/italic]",
        )

    console.print(table)
    console.print("\n[dim]BLOCK = fix before merge. WARN = fix before ship. NOTE = track.[/dim]")


@click.command()
@click.option("--base", default="main", show_default=True,
              help="Base branch for diff (ignored if --staged or --module).")
@click.option("--staged", is_flag=True,
              help="Review staged changes (pre-commit mode).")
@click.option("--module", "module_name", default=None,
              help="Review a specific module spec instead of a diff.")
@click.option("--json", "output_json", is_flag=True,
              help="Output raw JSON.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
@click.option("--fail-on-block", is_flag=True,
              help="Exit 1 if any BLOCK findings. For CI use.")
def architect_review(
    base: str,
    staged: bool,
    module_name: str | None,
    output_json: bool,
    project_path: str | None,
    fail_on_block: bool,
):
    """Architecture principle review against project persona + KB (advisory)."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    persona = _load_persona(pcp_dir)
    architecture = (pcp_dir / "architecture.md").read_text() if (pcp_dir / "architecture.md").exists() else ""

    if module_name:
        spec_text = _load_module_spec(pcp_dir, module_name)
        if not spec_text:
            console.print(f"[red]Error:[/red] No spec found for module '{module_name}'")
            sys.exit(2)
        kb = _load_kb(pcp_dir, [module_name])
        mode = "module"
        target = spec_text
        label = f"module:{module_name}"
    else:
        if staged:
            diff = _get_staged_diff()
            label = "staged changes"
        else:
            diff = _get_diff(base)
            label = f"diff vs {base}"

        if not diff.strip():
            console.print(f"[dim]No changes to review ({label}).[/dim]")
            sys.exit(0)

        changed_files = _changed_files_from_diff(diff)
        kb = _load_kb(pcp_dir, changed_files)
        mode = "diff"
        target = diff

    if not persona and not architecture:
        console.print(
            "[yellow]Warning:[/yellow] No architect_persona.md and no architecture.md found. "
            "Review will be generic. Run [cyan]pcp init[/cyan] to scaffold persona."
        )

    if not output_json:
        console.print(f"[dim]Reviewing {label}...[/dim]")

    try:
        result = llm.call_json(
            SYSTEM_PROMPT,
            _build_prompt(persona, architecture, kb, target, mode),
            model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="architect-review",
        )
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]LLM returned invalid JSON:[/red] {e}")
        sys.exit(2)

    # Same gap class as `pcp verify`: this command is called directly by a
    # harness-driven agent (pcp build-plan + the Workflow tool's agent()), not
    # only through build.py's own _run_architect_review wrapper -- which was
    # the only place this gate's telemetry got recorded before. Not a removed
    # hook, an omission: it was only ever wired for the older path.
    from pcp import telemetry
    telemetry.record(
        pcp_dir, cycle="qa", cycle_number=None, check="architect-review", control_id=None,
        module=module_name, submodule=None, criterion_id=None,
        files=changed_files if mode == "diff" else [],
        result="block" if result.get("blocks", 0) > 0 else "pass",
        errors=[f.get("finding", "") for f in result.get("findings", []) if f.get("severity") == "BLOCK"],
        error_count=result.get("blocks", 0),
    )

    if output_json:
        click.echo(json.dumps(result, indent=2))
        if fail_on_block and result.get("blocks", 0) > 0:
            sys.exit(1)
        sys.exit(0)

    _render_table(result)

    if fail_on_block and result.get("blocks", 0) > 0:
        sys.exit(1)
    sys.exit(0)
