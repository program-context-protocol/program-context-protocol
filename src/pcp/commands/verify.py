"""pcp verify — the missing "this is genuinely done, record it" command.

Root-caused 2026-07-30: 12 criteria on ontology-foundry read `status: complete`
with `verified_by: None`. `verified_by` is stamped ONLY by `_mark_criterion_complete`
inside `pcp build`'s real gated loop, so its absence on a `complete` criterion means
the status was hand-edited into `acceptance.yaml` directly.

That happened because there was no other path. `pcp pm` regenerates criteria from
a natural-language intent -- it is a spec-authoring tool, not a "record this as
done" tool, and using it to flip one field is both the wrong instrument and not
what anyone actually reached for. People opened the YAML instead, which is exactly
the failure Hard Rule 2 exists to prevent, and it left the exact ambiguity
`verified_by` was invented to remove: PCP can no longer tell "passed every gate"
from "someone edited the file".

This command re-runs the criterion's own deterministic check
(`file_exists`/`ast_pattern`, via `scan.py`'s existing `_evaluate_criterion`) where
one exists, and requires an explicit `--reason` plus confirmation where it does not
(`manual`/`test_passes`/UAT checks) -- the same accountability posture as
`[pcp-bypass: reason]` and `pcp objective-conflicts --dismiss`, never a silent flip.
Every verification is logged to `decision_log.jsonl`, so "why does PCP believe this
is done" has an answer PCP itself wrote down, and it is human-approved: it only
writes `acceptance.yaml` after producing a real diff and getting confirmation, the
same propose->diff->approve->write shape as `pcp amend`.
"""

import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import load_yaml

console = Console()


def _find_criterion(pcp_dir: Path, module: str, criterion_id: str) -> tuple[Path, dict, dict] | None:
    """(acc_path, acc_data, criterion) or None if not found."""
    acc_path = pcp_dir / "strategy" / "modules" / module / "acceptance.yaml"
    if not acc_path.exists():
        return None
    acc_data = load_yaml(acc_path) or {}
    for c in acc_data.get("criteria", []) or []:
        if c.get("id") == criterion_id:
            return acc_path, acc_data, c
    return None


def _evaluate(pcp_dir: Path, project_root: Path, module: str, criterion: dict) -> tuple[str, str]:
    """Reuses scan.py's own evaluator so 'verify' and 'scan' never disagree
    about what a check means -- two independent judges of the same fact is
    how the pending/complete mismatch happened in the first place."""
    from pcp.commands.scan import _evaluate_criterion
    spec_path = pcp_dir / "strategy" / "modules" / module / "spec.yaml"
    spec = load_yaml(spec_path) if spec_path.exists() else {}
    return _evaluate_criterion(criterion, module, project_root, {}, spec, pcp_dir)


@click.command()
@click.argument("module")
@click.argument("criterion_id")
@click.option("--reason", default=None,
              help="Required for manual/test_passes/UAT checks -- why this is genuinely done.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def verify(module: str, criterion_id: str, reason: str | None, yes: bool, project_path: str | None):
    """Record that MODULE's CRITERION_ID is genuinely built and complete.

    The gated write path for a criterion's status -- never hand-edit
    acceptance.yaml. Deterministic checks (file_exists, ast_pattern) are
    re-verified automatically; manual/test_passes/UAT checks require --reason.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    project_root = pcp_dir.parent

    found = _find_criterion(pcp_dir, module, criterion_id)
    if not found:
        console.print(f"[red]Error:[/red] no criterion '{criterion_id}' found in module '{module}'.")
        sys.exit(2)
    acc_path, acc_data, criterion = found

    if criterion.get("status") == "complete" and criterion.get("verified_by"):
        console.print(
            f"[green]{module}/{criterion_id}[/green] is already complete, "
            f"verified by [dim]{criterion['verified_by']}[/dim]. Nothing to do."
        )
        return

    check = criterion.get("check", "manual")
    deterministic = check in ("file_exists", "ast_pattern", "url_responds", "dom_contains", "visual")

    if deterministic:
        status, detail = _evaluate(pcp_dir, project_root, module, criterion)
        if status != "complete":
            console.print(
                f"[red]Refused:[/red] {module}/{criterion_id} declares check: {check}, "
                f"and re-running it says NOT complete:"
            )
            console.print(f"  [dim]{detail}[/dim]")
            console.print(
                "\n[dim]This is what `pcp verify` is for -- a deterministic check disagreeing "
                "with a hand-set 'complete' status is exactly the ambiguity it exists to catch.[/dim]"
            )
            sys.exit(1)
        console.print(f"[green]Deterministic check ({check}) confirms it:[/green] {detail}")
        source = f"pcp_verify:{check}"
    else:
        if not reason or not reason.strip():
            console.print(
                f"[red]Error:[/red] {module}/{criterion_id} declares check: {check}, which has no "
                "deterministic re-check. --reason is required -- state the concrete evidence "
                "(which tests pass, which commit, what you observed), the same accountability "
                "[pcp-bypass: reason] requires for a bypass."
            )
            sys.exit(2)
        source = "pcp_verify:manual"

    console.print(f"\n[bold]{module}/{criterion_id}[/bold]: {criterion.get('description', '')}")
    console.print(f"  check: {check}   current status: {criterion.get('status')}")
    if reason:
        console.print(f"  reason: {reason}")

    if not yes and not click.confirm("\nMark this criterion complete?", default=False):
        console.print("[yellow]Not marked. No changes written.[/yellow]")
        return

    for c in acc_data.get("criteria", []):
        if c.get("id") == criterion_id:
            c["status"] = "complete"
            c["verified_by"] = source
    acc_path.write_text(__import__("yaml").dump(acc_data, default_flow_style=False))

    from pcp import decision_log
    decision_log.record(
        pcp_dir, source="pcp_verify", category="manual-verification",
        module=module, criterion_id=criterion_id,
        summary=f"{module}/{criterion_id} marked complete via pcp verify ({source})",
        evidence=reason or "deterministic check re-run",
    )

    console.print(f"[green]✓[/green] {module}/{criterion_id} marked complete, verified_by={source}.")
    console.print("[dim]Recorded in decision_log.jsonl. Run `pcp scan` to refresh current_state.md.[/dim]")
