"""pcp init — scaffold .pcp/ directory in a project."""

import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

OBJECTIVE_TEMPLATE = """\
# Program Objective

## Why This Exists

[Describe the problem this program solves.]

## What Success Looks Like

1. [Measurable outcome 1]
2. [Measurable outcome 2]

## Out of Scope

- [What this program does NOT do]
"""

TARGET_STATE_TEMPLATE = """\
# Target State

[Describe what "done" looks like from a user/business perspective.
This is the ideal end state of the entire program.]
"""

ARCHITECTURE_TEMPLATE = """\
# Architecture

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| | | |

## Key Constraints

- [Constraint 1]

## Design Decisions

- [Decision and rationale]
"""

CI_RULES_TEMPLATE = """\
version: "1.0"
rules:
  - id: R001
    name: "Example: no hardcoded secrets"
    check: ast_pattern
    pattern: "(password|secret|api_key)\\\\s*=\\\\s*['\\\"][^'\\\"]{8,}['\\\"]"
    severity: hard_block
"""

SDLC_PHASE_TEMPLATE = """\
version: "1.0"
current_phase: alpha
phases:
  - name: alpha
    exit_criteria:
      - id: E001
        description: "Core functionality implemented"
        check: manual
        status: pending
"""

DECOMPOSITION_TEMPLATE = """\
# Strategy Decomposition

## How the Objective Breaks Down

[Explain how the program objective decomposes into modules and why.]

## Module Dependency Order

1. [module-a] — [reason]
2. [module-b] — depends on module-a

## Inter-Module Contracts

[Describe what each module provides to others.]
"""

MODULE_SPEC_TEMPLATE = """\
version: "1.0"
module: {name}
description: "What this module does (10 words minimum)."
objective_coverage:
  - "Which part of objective.md this covers"
dependencies: []
constraints:
  - "List constraints here"
"""

MODULE_ACCEPTANCE_TEMPLATE = """\
version: "1.0"
module: {name}
criteria:
  - id: A001
    description: "Core implementation exists"
    check: manual
    status: pending
"""


def _write(path: Path, content: str, force: bool) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


@click.command()
@click.option("--path", "project_path", type=click.Path(), default=".",
              help="Project root (default: current directory).")
@click.option("--module", "module_name", default=None,
              help="Also scaffold a module under strategy/modules/<name>/.")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def init(project_path: str, module_name: str | None, force: bool):
    """Scaffold .pcp/ directory in a project."""
    root = Path(project_path).resolve()
    pcp = root / ".pcp"

    files = {
        pcp / "objective.md": OBJECTIVE_TEMPLATE,
        pcp / "target_state.md": TARGET_STATE_TEMPLATE,
        pcp / "architecture.md": ARCHITECTURE_TEMPLATE,
        pcp / "ci_rules.yaml": CI_RULES_TEMPLATE,
        pcp / "SDLC_phase.yaml": SDLC_PHASE_TEMPLATE,
        pcp / "strategy" / "decomposition.md": DECOMPOSITION_TEMPLATE,
    }

    if module_name:
        mod_dir = pcp / "strategy" / "modules" / module_name
        files[mod_dir / "spec.yaml"] = MODULE_SPEC_TEMPLATE.format(name=module_name)
        files[mod_dir / "acceptance.yaml"] = MODULE_ACCEPTANCE_TEMPLATE.format(name=module_name)

    created = []
    skipped = []
    for path, content in files.items():
        if _write(path, content, force):
            created.append(path.relative_to(root))
        else:
            skipped.append(path.relative_to(root))

    for p in created:
        console.print(f"  [green]created[/green]  {p}")
    for p in skipped:
        console.print(f"  [dim]skipped[/dim]  {p}  (exists, use --force to overwrite)")

    gitattributes = root / ".gitattributes"
    ga_lines = [
        ".pcp/current_state.md merge=ours",
        ".pcp/diff.md merge=ours",
        ".pcp/bypass_log.yaml merge=union",
    ]
    existing = gitattributes.read_text() if gitattributes.exists() else ""
    additions = [l for l in ga_lines if l not in existing]
    if additions:
        with open(gitattributes, "a") as f:
            f.write("\n" + "\n".join(additions) + "\n")
        console.print(f"  [green]updated[/green]  .gitattributes")

    console.print(f"\n[bold]PCP initialised at {pcp}[/bold]")
    console.print("\nNext steps:")
    console.print("  1. Edit [cyan].pcp/objective.md[/cyan] — describe WHY this program exists")
    console.print("  2. Edit [cyan].pcp/strategy/decomposition.md[/cyan] — break objective into modules")
    console.print("  3. Run [cyan]pcp init --module <name>[/cyan] for each module")
    console.print("  4. Run [cyan]pcp validate-strategy[/cyan] to check coverage")
