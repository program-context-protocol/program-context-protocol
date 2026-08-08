"""pcp inspiration-art — the human-authorized write path for
.pcp/strategy/inspiration_art.md.

Closes a gap traced during a design conversation 2026-08-07: `capabilities_
enumerated` (what kickoff/pm decompose into modules) comes ONLY from what the
PM types into the vision/intent text -- nothing external grounds it, so an
omitted-but-standard capability (an MDM tool with no compliance module, a
migration tool with no rollback path) is silently absent, not flagged. This
command records a product's category(ies) and each one's reference module +
screen shape, so kickoff/pm have something outside the PM's own words to
check `capabilities_enumerated` against, and a later gap (via pm's own
`check_capability_coverage`) has somewhere to route instead of just a
console warning nobody acts on.

A product is rarely 1-1 with a single category -- most are many categories
to one product (a migration tool that's also a compat layer that's also a
release-orchestration system). Each run proposes candidate categories (the
initial seed, or one more to cover a `--gap`), never auto-applies one.

Scope note, deliberate: this command's own LLM call has NO web-search/tool-
use capability (see llm/client.py's module docstring -- call_json() is a
single-shot CLI dispatch, and PCP does not rebuild agentic search infra,
same standing rule as everywhere else in this repo). So its draft is
explicitly labeled recall-only/unverified. The interactive `/pcp` skill's
Inspiration-Art Research step (real WebSearch/Agent access) is where a
citation-backed draft actually gets produced before a human approves it --
this command still works standalone/headless (CI, scripted use), honestly
low-confidence when used that way.
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp import spec_write
from pcp.spec_write import SpecTarget

console = Console()

# Must stay in sync with module_acceptance.schema.json's screen_archetypes
# enum -- inlined here the same way kickoff.py inlines its own check/status
# enums into its prompt rather than reading the schema file at prompt-build
# time (see kickoff.py's _comment_criteria_enums for the precedent).
SCREEN_ARCHETYPES = (
    "dashboard, data_entry_form, list_table, detail_view, search_filter, "
    "settings, chat, canvas_editor, wizard, auth, other"
)

SYSTEM_PROMPT = f"""\
You are a product architect identifying which established product category(ies) \
a project resembles, and what that category's reference architecture typically \
includes -- so a PM can catch an omitted-but-standard capability before it \
becomes a silent gap.

A product is rarely 1-1 with a single category. Propose EVERY category that \
genuinely applies to a distinct subset of the described product -- do not force \
one category to cover the whole thing if it doesn't.

For each category, give:
- Typical modules/capabilities products in this category have, however small.
- Typical screens, using ONLY these screen_archetype values (do not invent new \
ones): {SCREEN_ARCHETYPES}. "other" is the honest escape hatch when nothing fits.
- source_evidence: list what you're drawing on. You have NO live search access in \
this call -- always state "training-data recall, unverified" explicitly as one \
entry; do not claim to have searched anything.

If a --gap capability is given, propose exactly ONE category that would plausibly \
cover it, not a general sweep.

You must output ONLY valid JSON — no prose, no markdown, no code fences.

Output schema:
{{
  "inspiration_art_md": "full replacement content of inspiration_art.md, markdown, \
one '## <Category Name>' section per category with Source evidence / Typical \
modules / Typical screens / Covers modules subsections -- preserve any existing \
categories already in the current file content given below, add the new one(s)",
  "summary": "one paragraph: which category(ies) proposed and why, for the audit trail"
}}
"""


@click.command("inspiration-art")
@click.argument("description", required=False, default=None)
@click.option("--gap", "gap", default=None, metavar="CAPABILITY",
              help="Propose one category to cover a specific uncovered capability (see `pcp pm`'s coverage warning), instead of a general seed sweep.")
@click.option("--path", "project_path", type=click.Path(), default=None)
@click.option("--yes", "yes", is_flag=True, help="Skip the interactive diff-approval prompt (scripted/CI use).")
def inspiration_art(description: str | None, gap: str | None, project_path: str | None, yes: bool):
    """Propose + human-approve category reference architecture(s) for this project."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    if not description and not gap:
        console.print("[red]Error:[/red] pass a description of what you're building, or --gap \"<capability>\"")
        sys.exit(2)

    art_path = pcp_dir / "strategy" / "inspiration_art.md"
    objective_path = pcp_dir / "objective.md"

    if gap:
        intent = f"Find one category covering this uncovered capability: {gap}"
        task_line = f"## Uncovered capability to cover\n{gap}"
    else:
        intent = description
        task_line = f"## What's being built\n{description}"

    user_prompt = "\n\n".join([
        task_line,
        f"## Program objective (if any)\n{objective_path.read_text() if objective_path.exists() else '(not written yet)'}",
        f"## Current inspiration_art.md content\n{art_path.read_text() if art_path.exists() else '(empty -- no categories researched yet)'}",
    ])

    outcome = spec_write.propose_and_write(
        pcp_dir,
        [SpecTarget(name="strategy/inspiration_art.md", path=art_path, key="inspiration_art_md")],
        SYSTEM_PROMPT,
        user_prompt,
        command="inspiration-art",
        intent=intent,
        yes=yes,
        decision_category="architecture",
        no_change_hint="If this is unexpected, restate what's being built more concretely.",
    )
    if not outcome.written:
        sys.exit(0)

    console.print(
        "\n[dim]Recall-only draft if run headlessly -- for real citations, run this from an "
        "interactive /pcp session (Inspiration-Art Research step) instead, or verify the "
        "source_evidence above yourself before treating it as researched fact.[/dim]"
    )
