"""pcp verify — the missing "this is genuinely done, record it" command.

Root-caused 2026-07-30: 12 criteria on Project O read `status: complete`
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
(`manual`/UAT checks) -- the same accountability posture as `[pcp-bypass: reason]`
and `pcp objective-conflicts --dismiss`, never a silent flip.

`test_passes` used to be the exception that mattered most: scan.py's own evaluator
just preserves whatever status was already there ("test_passes: preserved"), so it
was the ONE check type with zero deterministic re-verification -- and the exact
category real fake tests live in (2026-08-08: PCP's own dogfood found a test suite
97% source-text-grep, reading as fully "verified"). A `test_passes` criterion that
declares a `target` pointing at a real test file/function now gets the same
treatment: the test is actually re-run, AND checked for the fake shapes
test_composition.py's own classifiers catch (grep-shaped, assertion-free,
self-mocked -- patches the exact symbol it then asserts against). Refused unless
`--allow-weak-test "<reason>"` overrides, logged distinctly to `decision_log.jsonl`
(category `weak-test-override`) -- same accountability posture as a bypass, never
silent, and has no effect on a genuine test FAILURE (only a fake-shape refusal). A
`test_passes` criterion with no `target` declared keeps the original manual+--reason
path unchanged, fully backward compatible.

Every verification is logged to `decision_log.jsonl`, so "why does PCP believe this
is done" has an answer PCP itself wrote down, and it is human-approved: it only
writes `acceptance.yaml` after producing a real diff and getting confirmation, the
same propose->diff->approve->write shape as `pcp amend`.
"""

import ast
import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp import chain_guard

console = Console()

_TEST_PASSES_TIMEOUT_SEC = 300


def _resolve_test_target(target: str) -> tuple[str, str | None]:
    """'path/to/test_file.py::test_name' -> ('path/to/test_file.py', 'test_name').
    A plain file path -> (path, None)."""
    if "::" in target:
        file_part, name_part = target.split("::", 1)
        return file_part, name_part
    return target, None


def _is_test_file_path(path: str) -> bool:
    name = Path(path).name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")


def _evaluate_test_passes(pcp_dir: Path, project_root: Path, criterion: dict) -> tuple[str, str, bool]:
    """(status, detail, refused_as_fake). Only called when criterion declares
    a `target` pointing at a real Python test file/function.

    `test_passes` previously had ZERO deterministic re-check -- scan.py's own
    evaluator just preserves whatever status was already there
    ("test_passes: preserved (run tests to update)"), making it the weakest
    check type in the whole schema, and exactly the category real fake tests
    live in. Two stages: (1) actually run the declared test and confirm it
    currently passes; (2) even a passing test can be fake -- reuse this
    session's own test_composition.py classifiers (grep-shaped /
    assertion-free / self-mocked) on the specific function this criterion
    points at, and refuse if it's any of the three fake shapes. Same
    detection already free and always-on in `pcp audit`, applied here as a
    real gate instead of only a report."""
    from pcp.qa import project_tool
    from pcp.test_composition import classify_test_function, has_any_assertion, calls_only_its_own_patched_target

    target = criterion.get("target", "")
    file_part, test_name = _resolve_test_target(target)
    full_path = project_root / file_part

    if not full_path.exists():
        return criterion.get("status", "pending"), f"declared test target '{target}' does not exist", False

    pytest_bin = project_tool(project_root, "pytest")
    if not pytest_bin:
        return criterion.get("status", "pending"), "test_passes: no pytest detected -- falling back to prior status", False

    node_id = str(full_path) + (f"::{test_name}" if test_name else "")
    try:
        result = subprocess.run(
            [pytest_bin, "-q", node_id], capture_output=True, text=True,
            cwd=project_root, timeout=_TEST_PASSES_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return "pending", f"declared test '{target}' timed out re-running", False

    if result.returncode not in (0, 5):
        tail = (result.stdout + result.stderr)[-1500:]
        return "pending", f"declared test '{target}' currently FAILS:\n{tail}", False

    try:
        tree = ast.parse(full_path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return "complete", f"test '{target}' passes (composition check skipped -- file did not parse)", False

    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
        and (test_name is None or n.name == test_name)
    ]
    if not funcs:
        return "complete", f"test '{target}' passes (composition check skipped -- function not found for AST analysis)", False

    fake_reasons = []
    for func in funcs:
        if not has_any_assertion(func):
            fake_reasons.append(f"`{func.name}` has zero assertions")
        elif calls_only_its_own_patched_target(func):
            fake_reasons.append(f"`{func.name}` only calls its own patched target (self-mocked)")
        elif classify_test_function(func) == "grep_shaped":
            fake_reasons.append(f"`{func.name}` is source-grep-shaped (checks a name exists, not behavior)")

    if test_name is None and fake_reasons and len(fake_reasons) < len(funcs):
        # Whole-file target with a MIXED result -- at least one real test
        # exists in the file, so don't refuse on the coarse whole-file bar.
        fake_reasons = []

    if fake_reasons:
        return "pending", f"declared test '{target}' passes but is FAKE-SHAPED: " + "; ".join(fake_reasons), True

    return "complete", f"test '{target}' passes and is real-execution.", False


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
@click.option("--allow-weak-test", default=None,
              help="Override a test_passes refusal caused by a structurally fake test (grep-shaped / "
                   "assertion-free / self-mocked) -- state why this is acceptable anyway. Logged like a "
                   "bypass, never silent. Has no effect on a genuine test FAILURE, only a fake-shape refusal.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def verify(module: str, criterion_id: str, reason: str | None, allow_weak_test: str | None,
           yes: bool, project_path: str | None):
    """Record that MODULE's CRITERION_ID is genuinely built and complete.

    The gated write path for a criterion's status -- never hand-edit
    acceptance.yaml. Deterministic checks (file_exists, ast_pattern) are
    re-verified automatically. test_passes WITH a `target` pointing at a
    test file is also re-verified automatically -- re-run and checked for
    fake shapes (grep-shaped/assertion-free/self-mocked); refused unless
    --allow-weak-test overrides. manual/UAT/test_passes-without-a-target
    require --reason.
    """
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)
    project_root = pcp_dir.parent

    try:
        chain_guard.assert_chain_integrity(pcp_dir)
    except chain_guard.ChainIntegrityError as e:
        console.print(f"[red]Error:[/red] {e}")
        console.print("[dim]Run `pcp provenance` for the full break detail. Not writing on top of a tampered record.[/dim]")
        sys.exit(2)

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
    target = criterion.get("target", "")
    test_file_part = _resolve_test_target(target)[0] if target else ""
    test_passes_gated = check == "test_passes" and bool(target) and _is_test_file_path(test_file_part)

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
    elif test_passes_gated:
        status, detail, refused_as_fake = _evaluate_test_passes(pcp_dir, project_root, criterion)
        if status != "complete":
            overridden = refused_as_fake and bool(allow_weak_test and allow_weak_test.strip())
            if not overridden:
                console.print(
                    f"[red]Refused:[/red] {module}/{criterion_id} declares check: test_passes, "
                    f"target: {target}"
                )
                console.print(f"  [dim]{detail}[/dim]")
                if refused_as_fake:
                    console.print(
                        "\n[dim]The declared test passes but is structurally fake (grep-shaped / "
                        "assertion-free / self-mocked -- the same classifiers `pcp audit` already "
                        "reports). Pass --allow-weak-test \"<reason>\" to override -- logged like a "
                        "bypass, never silent.[/dim]"
                    )
                else:
                    console.print(
                        "\n[dim]This is what `pcp verify` is for -- a deterministic check disagreeing "
                        "with a hand-set 'complete' status is exactly the ambiguity it exists to catch.[/dim]"
                    )
                sys.exit(1)
            console.print(f"[yellow]Overridden via --allow-weak-test:[/yellow] {detail}")
            source = "pcp_verify:test_passes(weak-override)"
        else:
            console.print(f"[green]Deterministic re-check (test_passes) confirms it:[/green] {detail}")
            source = "pcp_verify:test_passes"
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
    if source.endswith("(weak-override)"):
        console.print(f"  override reason: {allow_weak_test}")

    if not yes and not click.confirm("\nMark this criterion complete?", default=False):
        console.print("[yellow]Not marked. No changes written.[/yellow]")
        return

    for c in acc_data.get("criteria", []):
        if c.get("id") == criterion_id:
            c["status"] = "complete"
            c["verified_by"] = source
    acc_path.write_text(__import__("yaml").dump(acc_data, default_flow_style=False))

    category = "manual-verification"
    evidence = reason or "deterministic check re-run"
    if source.endswith("(weak-override)"):
        category = "weak-test-override"
        evidence = allow_weak_test

    from pcp import decision_log
    decision_log.record(
        pcp_dir, source="pcp_verify", category=category,
        module=module, criterion_id=criterion_id,
        summary=f"{module}/{criterion_id} marked complete via pcp verify ({source})",
        evidence=evidence,
    )

    # Restores the build-cycle signal for work done through the native-harness
    # path (pcp build-plan + the Workflow tool's own agent()/parallel() -- see
    # CLAUDE.md's Workflow/Agent/pcp-build split). That path marks a criterion
    # done via `pcp verify` directly, never through build.py's own
    # _build_one_criterion, which is the ONLY place telemetry.record() used to
    # be called for build-cycle events. Nothing removed a hook; the hook was
    # only ever written for the older headless-engine path, so telemetry.jsonl
    # went silent the moment real work moved to the harness-driven one, even
    # though decision_log.jsonl (recorded above) kept going.
    from pcp import telemetry
    telemetry.record(
        pcp_dir, cycle="build", cycle_number=None,
        module=module, submodule=None, criterion_id=criterion_id, files=[],
        languages=[], lines_added=0, lines_removed=0,
        result="pass", source=source,
    )

    console.print(f"[green]✓[/green] {module}/{criterion_id} marked complete, verified_by={source}.")
    console.print("[dim]Recorded in decision_log.jsonl. Run `pcp scan` to refresh current_state.md.[/dim]")
