"""pcp enrich — research pass that finds commonly-expected features an
existing module is missing, then hands the result to `pcp pm`'s existing
gated write path.

Genuinely new capability, not a rename of something that already existed
(confirmed against CHANGELOG.md, all memory, and a full repo grep before
this was written — see the 2026-08-07 conversation this closes). Two
existing pieces get composed, nothing new is built underneath them:

1. Research: routed through the `agy` harness (`llm.client.call_json(...,
   harness="agy")`), already wired in llm/client.py for exactly this kind
   of task — CLAUDE.md's LLM Routing table already names "Research /
   validation" as agy's job, this is the first call site that actually
   dispatches a research prompt through it rather than agy's prior sole use
   as build.py's cross-vendor BLOCK-finding verifier.
2. Write: the researched features become an intent string handed straight
   to the existing `pm` command via `ctx.invoke` — same reuse pattern
   pm.py's own `_warn_stale_decomposition` already uses to invoke `amend`.
   `enrich` writes nothing itself; every check pm already runs (decompose-
   first, capability-coverage, prior-art-evidence, validate-strategy, the
   human diff/approve confirm) applies unchanged to the researched features.

No new subprocess/CLI integration, no new write path — the "no native-
harness duplication" rule (see memory) stays intact.
"""

import sys
from pathlib import Path

import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.llm import client as llm

console = Console()

RESEARCH_SYSTEM_PROMPT = """\
You are a product research analyst. Given a module's current capabilities \
and (if available) its researched product category, identify OTHER features \
commonly found in real, comparable products for a component of this shape — \
features this module does not have yet.

Stay inside this module's own category boundary — do not propose scope that \
belongs to a different module. Do not restate a capability that already \
exists (it is listed below). If genuinely nothing further is missing, return \
an empty list rather than padding it with marginal ideas.

For each feature, name what you are drawing on as source_evidence — a real \
product, standard, or pattern. If you have no live search grounding for a \
given feature, say so plainly ("training-data recall, unverified") instead \
of implying a citation you don't have.

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{
  "researched_features": [
    {
      "feature": "short name",
      "rationale": "why real products in this category commonly have it",
      "source_evidence": "what this is drawing on"
    }
  ],
  "summary": "one paragraph: what was researched and why these features, for the audit trail"
}
"""


def _module_context(pcp_dir: Path, module: str) -> tuple[str, str]:
    """Returns (category context, module context) for the research prompt.
    Raises FileNotFoundError if the module has no spec.yaml yet."""
    mod_dir = pcp_dir / "strategy" / "modules" / module
    spec_path = mod_dir / "spec.yaml"
    if not spec_path.exists():
        raise FileNotFoundError(module)

    spec = yaml.safe_load(spec_path.read_text()) or {}
    description = spec.get("description", "")
    category_ref = spec.get("category_reference") or {}

    acc_path = mod_dir / "acceptance.yaml"
    existing = []
    if acc_path.exists():
        acc = yaml.safe_load(acc_path.read_text()) or {}
        existing = [c.get("description", "") for c in acc.get("criteria", []) if isinstance(c, dict)]

    art_path = pcp_dir / "strategy" / "inspiration_art.md"
    category_context = "(no researched category reference for this project yet)"
    if category_ref.get("category"):
        category_context = (
            f"This module was traced to category '{category_ref['category']}' "
            f"({category_ref.get('rationale', '')}).\n"
        )
        if art_path.exists():
            category_context += f"\nFull researched categories on file:\n{art_path.read_text()}"
    elif art_path.exists():
        category_context = f"Researched categories on file (none explicitly traced to this module):\n{art_path.read_text()}"

    module_context = (
        f"## Module: {module}\n"
        f"Description: {description}\n\n"
        f"Existing capabilities (do not repeat these):\n"
        + "\n".join(f"- {d}" for d in existing if d)
    )
    return category_context, module_context


@click.command()
@click.argument("module")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def enrich(module: str, project_path: str | None):
    """Research commonly-expected features an existing module is missing, via agy, then route accepted ones through `pcp pm`."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    try:
        category_context, module_context = _module_context(pcp_dir, module)
    except FileNotFoundError:
        console.print(
            f"[red]Error:[/red] module '{module}' has no spec.yaml yet — "
            f"`pcp enrich` researches gaps in an EXISTING module. Create it first "
            f'via `pcp pm "<intent>"`.'
        )
        sys.exit(2)

    user_prompt = f"## Category context\n{category_context}\n\n{module_context}"

    console.print(f"[dim]Researching missing features for module '{module}' via agy...[/dim]")
    try:
        result = llm.call_json(
            RESEARCH_SYSTEM_PROMPT, user_prompt,
            pcp_dir=pcp_dir, command="enrich-research",
            harness="agy",
        )
    except RuntimeError as e:
        console.print(f"[red]Error calling agy:[/red] {e}")
        console.print(
            "[dim]`pcp enrich` requires the Antigravity CLI (`agy`) on PATH. "
            "Install it, or override the binary with PCP_AGY_BIN.[/dim]"
        )
        sys.exit(2)
    except ValueError as e:
        console.print(f"[red]agy returned invalid JSON:[/red] {e}")
        sys.exit(2)

    features = result.get("researched_features") or []
    if not features:
        console.print(f"[green]✓[/green] No missing features found for '{module}'. Nothing to do.")
        console.print(f"[dim]{result.get('summary', '')}[/dim]")
        return

    console.print(f"\n[bold]{len(features)} researched feature(s) for '{module}':[/bold]")
    for f in features:
        console.print(f"  - [bold]{f.get('feature', '(unnamed)')}[/bold]: {f.get('rationale', '')}")
        console.print(f"    [dim]source: {f.get('source_evidence', '')}[/dim]")
    console.print(f"\n[dim]{result.get('summary', '')}[/dim]\n")

    if not click.confirm(f"Route these {len(features)} feature(s) into `pcp pm` for criteria generation?"):
        console.print("[yellow]Aborted -- nothing written.[/yellow]")
        return

    intent_lines = [
        f"Enrich module '{module}' with these researched features (each as a new acceptance "
        f"criterion in module '{module}' unless a feature genuinely requires a different existing module):"
    ]
    for f in features:
        intent_lines.append(f"- {f.get('feature', '')}: {f.get('rationale', '')}")
    intent = "\n".join(intent_lines)

    from pcp.commands.pm import pm
    ctx = click.get_current_context(silent=True)
    if ctx:
        ctx.invoke(pm, intent=intent, project_path=str(pcp_dir.parent))
    else:
        console.print(f'[dim]Run: pcp pm "{intent}"[/dim]')
