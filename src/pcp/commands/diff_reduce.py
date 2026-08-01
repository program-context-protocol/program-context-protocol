"""pcp diff-reduce — Loop 2, the diff-reduction loop.

Reads the build capsule (run_ledger.jsonl + acceptance.yaml status +
validate-strategy's coverage_gaps), figures out what's still open against
spec, and drives it closed -- bounded and gated, not an unattended
while-True. Designed 2026-07-31 after the capsule fields (wave_number,
logic_tier, internal/external deps, real coupling) landed; this is the
first consumer of that data, not a parallel mechanism.

Five gates, each reusing an existing PCP mechanism rather than inventing
one:

  A. Round cap (3, PCP's existing 3-attempt convention, not the 6-round
     cap loop_until_dry_breakdown uses -- each round here triggers a real
     build, not a cheap Haiku call). Stops early the moment a round's plan
     is empty; no need for a 2-consecutive-dry pattern since "the diff is
     empty" is a deterministic fact here, not a judgment call that might
     find something on a second look.
  B. Human approval, split by risk:
       - existing pending criteria (already spec/pm-approved, just not
         built) -> straight to `pcp build`, no new gate needed.
       - a capability gap with NO criterion at all -> routed through
         `pcp pm`'s own existing confirm, unconditionally, every time.
         Only attempted when a human is actually attached (stdin is a
         tty) -- headless/cron runs report the gap and stop there, never
         auto-propose new scope unattended.
  C. Execution never bypasses `pcp build`'s own gate stack (test/lint/
     SAST/architect-review/wave-merge) -- called via its real callback,
     not reimplemented.
  D. Freshness re-check every round, not just once: objective_hash (existing
     mechanism) PLUS a per-module spec+acceptance content hash (same
     pattern, one level deeper) stamped at round start and re-checked at
     round end. A mid-round edit to either aborts the loop and escalates
     instead of continuing to build against a target that moved.
  E. Cost ceiling: none of its own. Inherits PCP_MAX_BUILD_SESSIONS /
     PCP_PROJECT_BUDGET_USD via the real `pcp build` call -- no separate
     spend surface.

Plus the absence-blindspot fix: before trusting any criterion's
`status: complete`, deterministic ones get spot-re-checked via the same
evaluator `pcp verify` uses (scan.py's _evaluate_criterion) -- "check
aimed at wrong target reads identical to a clean pass" is a confirmed
3x-recurring bug class in this codebase (Postgres port squat, pytest venv
resolution, --version metadata), and a diff computed from a stale
`status: complete` would silently never surface that gap for a human to
even see, let alone approve. A criterion that fails its own re-check gets
reopened (mirrors `_reopen_wave_criteria`'s existing shape) and one
escalation recorded per module -- never a silent flip.

And the round-cap-hit fix: if all `max_rounds` are used and the gap is
still open, that gets an explicit escalation too. A loop that stops
without saying so is advisory in practice, not really stopped (same
lesson as the wave-BLOCK-left-work-marked-verified bug).
"""

import hashlib
import sys
from pathlib import Path

import click
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import load_yaml
from pcp import escalations
from pcp import run_log

console = Console()

DEFAULT_MAX_ROUNDS = 3


def _module_spec_hash(pcp_dir: Path, module_name: str) -> str:
    """Same pattern as objective_conflicts.objective_hash, one level deeper
    -- a fingerprint of the files a human would need to edit for this
    module's own scope to have genuinely changed.

    acceptance.yaml's `status`/`verified_by` fields are stripped before
    hashing -- those are exactly what THIS LOOP's own build step is
    expected to write every round (a completed criterion), so including
    them would make Gate D fire a false "drift" on every single successful
    round. What's still watched: the criteria list itself (a human adding/
    removing/editing a criterion via `pcp pm` mid-loop is real drift), and
    spec.yaml in full (protected, never touched by a build)."""
    mod_dir = pcp_dir / "strategy" / "modules" / module_name
    spec_path = mod_dir / "spec.yaml"
    spec_text = spec_path.read_text() if spec_path.exists() else ""

    acc_path = mod_dir / "acceptance.yaml"
    acc_text = ""
    if acc_path.exists():
        acc = load_yaml(acc_path) or {}
        stripped = [
            {k: v for k, v in c.items() if k not in ("status", "verified_by")}
            for c in (acc.get("criteria") or [])
        ]
        import json
        acc_text = json.dumps(stripped, sort_keys=True)

    return hashlib.sha256("\x00".join([spec_text, acc_text]).encode()).hexdigest()


def _in_scope_module_names(pcp_dir: Path, module_name: str | None) -> list[str]:
    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        return []
    if module_name:
        return [module_name] if (modules_dir / module_name).exists() else []
    return sorted(p.name for p in modules_dir.iterdir() if p.is_dir())


def _freshness_stamp(pcp_dir: Path, module_names: list[str]) -> dict:
    from pcp import objective_conflicts
    return {
        "objective_hash": objective_conflicts.objective_hash(pcp_dir),
        "module_hashes": {m: _module_spec_hash(pcp_dir, m) for m in module_names},
    }


def _freshness_drifted(pcp_dir: Path, stamp: dict, module_names: list[str]) -> str | None:
    """Returns a human-readable reason if something moved since `stamp` was
    taken, else None. Checked, never silently trusted -- see Gate D."""
    from pcp import objective_conflicts
    if objective_conflicts.objective_hash(pcp_dir) != stamp["objective_hash"]:
        return "objective.md/target_state.md changed mid-round"
    for m in module_names:
        if _module_spec_hash(pcp_dir, m) != stamp["module_hashes"].get(m):
            return f"module '{m}'s spec.yaml/acceptance.yaml changed mid-round"
    return None


def _spot_check_complete_criteria(pcp_dir: Path, project_root: Path, module_names: list[str]) -> list[str]:
    """Absence-blindspot fix: re-run each deterministic 'complete' criterion's
    own check (verify.py's evaluator, so this and `pcp verify` never
    disagree about what a check means) before trusting the diff computed
    from acceptance.yaml. A criterion whose re-check now fails gets
    reopened -- status back to pending, verified_by cleared -- because a
    diff computed from a status that's silently gone stale is exactly the
    failure this loop exists to close, not repeat. Returns the list of
    reopened 'module/criterion_id' strings."""
    from pcp.commands.verify import _evaluate

    _DETERMINISTIC_CHECKS = {"file_exists", "ast_pattern", "url_responds", "dom_contains", "visual"}
    reopened: list[str] = []

    for mod_name in module_names:
        acc_path = pcp_dir / "strategy" / "modules" / mod_name / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path) or {}
        criteria = acc.get("criteria", []) or []
        mod_reopened: list[str] = []
        for c in criteria:
            if c.get("status") != "complete":
                continue
            check = c.get("check", "manual")
            if check not in _DETERMINISTIC_CHECKS:
                continue  # manual/test_passes have no re-check -- can't spot-check these, not a gap this pass can close
            try:
                status, detail = _evaluate(pcp_dir, project_root, mod_name, c)
            except Exception:
                continue  # evaluator itself failing isn't evidence the criterion is wrong -- don't compound one failure into a false reopen
            if status != "complete":
                c["status"] = "pending"
                c.pop("verified_by", None)
                mod_reopened.append(c["id"])
                reopened.append(f"{mod_name}/{c['id']}")
        if mod_reopened:
            acc_path.write_text(__import__("yaml").dump(acc, default_flow_style=False))
            escalations.record(
                pcp_dir, mod_name, "diff-reduce-spotcheck",
                route="diff-reduce-reopen",
                findings=[
                    f"Re-running {mod_name}/{cid}'s own deterministic check no longer confirms it "
                    "complete -- reopened by pcp diff-reduce's spot-check before planning this round."
                    for cid in mod_reopened
                ],
            )
    return reopened


def _compute_gap(pcp_dir: Path, module_name: str | None) -> dict:
    """One round's view of what's still open. `pending` reuses build.py's
    own gathering function (kept in sync with `pcp build` by construction);
    coverage_gaps/missing_modules only make sense project-wide, so they're
    skipped when scoped to one module."""
    from pcp.commands.build import gather_modules_to_build
    pending_modules = gather_modules_to_build(pcp_dir, module_name)
    pending_count = sum(len(m["pending_criteria"]) for m in pending_modules)

    coverage_gaps: list[dict] = []
    missing_modules: list[dict] = []
    if module_name is None:
        from pcp.commands.validate_strategy import run_validate_strategy
        try:
            val = run_validate_strategy(pcp_dir)
        except Exception as e:
            val = None
            console.print(f"[dim]diff-reduce: validate-strategy skipped this round ({e})[/dim]")
        if val:
            coverage_gaps = val.get("coverage_gaps", []) or []
            missing_modules = val.get("missing_modules", []) or []

    return {
        "pending_modules": pending_modules, "pending_count": pending_count,
        "coverage_gaps": coverage_gaps, "missing_modules": missing_modules,
    }


def run_diff_reduce(
    pcp_dir: Path, project_root: Path, module_name: str | None = None,
    yes: bool = False, max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> dict:
    """Reusable core -- returns a summary dict. The CLI command below is a
    thin wrapper, same shape as run_validate_strategy/build's own split."""
    rounds_run = 0
    stopped_reason = "not started"
    interactive = sys.stdin.isatty()

    for round_num in range(1, max_rounds + 1):
        rounds_run = round_num
        module_names = _in_scope_module_names(pcp_dir, module_name)

        # Gate: concurrent-writer check (open_runs already exists for
        # exactly this -- a PRE record with no matching POST is a run
        # still in progress).
        open_now = run_log.open_runs(run_log.load(pcp_dir))
        colliding = [r for r in open_now if r.get("module") in module_names]
        if colliding:
            stopped_reason = f"another run is open on module(s) {sorted({r['module'] for r in colliding})} -- not racing it"
            console.print(f"[yellow]diff-reduce: {stopped_reason}[/yellow]")
            break

        # Gate D, start-of-round stamp.
        stamp = _freshness_stamp(pcp_dir, module_names)

        # Absence-blindspot spot-check, before the gap is even computed.
        reopened = _spot_check_complete_criteria(pcp_dir, project_root, module_names)
        if reopened:
            console.print(f"[yellow]diff-reduce: spot-check reopened {len(reopened)} criteria that no longer pass their own check: {reopened}[/yellow]")

        gap = _compute_gap(pcp_dir, module_name)
        console.print(
            f"\n[bold]diff-reduce round {round_num}/{max_rounds}:[/bold] "
            f"{gap['pending_count']} pending criteria, "
            f"{len(gap['coverage_gaps'])} coverage gap(s), {len(gap['missing_modules'])} missing module(s)"
        )

        if not gap["pending_count"] and not gap["coverage_gaps"] and not gap["missing_modules"]:
            stopped_reason = "dry -- nothing open"
            break

        # Gate B, expansion half: new criteria only ever proposed with a
        # human attached, and only through pm's own unconditional confirm.
        if (gap["coverage_gaps"] or gap["missing_modules"]) and interactive:
            from pcp.commands.pm import pm as pm_cmd
            for g in gap["coverage_gaps"]:
                intent = f"Cover this gap found by validate-strategy: {g.get('area', '')} -- {g.get('quote', '')}"
                try:
                    pm_cmd.callback(intent=intent, project_path=str(project_root))
                except SystemExit as e:
                    if e.code:
                        console.print(f"[dim]diff-reduce: pm step for coverage gap exited ({e.code}), continuing[/dim]")
            for g in gap["missing_modules"]:
                intent = f"Add missing module found by validate-strategy: {g.get('name', '')} -- {g.get('reason', '')}"
                try:
                    pm_cmd.callback(intent=intent, project_path=str(project_root))
                except SystemExit as e:
                    if e.code:
                        console.print(f"[dim]diff-reduce: pm step for missing module exited ({e.code}), continuing[/dim]")
            gap = _compute_gap(pcp_dir, module_name)
        elif gap["coverage_gaps"] or gap["missing_modules"]:
            console.print(
                f"[dim]diff-reduce: {len(gap['coverage_gaps'])} coverage gap(s)/{len(gap['missing_modules'])} "
                "missing module(s) found, but no human is attached (non-interactive run) -- not "
                "proposing new scope unattended. Run `pcp pm`/`pcp kickoff` yourself, or re-run "
                "diff-reduce interactively.[/dim]"
            )

        if not gap["pending_count"]:
            stopped_reason = "dry after this round's coverage-gap pass -- nothing buildable"
            break

        # Gate B, existing-pending half + the round's own spend confirm.
        if not yes:
            names = ", ".join(f"{m['name']} ({len(m['pending_criteria'])})" for m in gap["pending_modules"])
            if not click.confirm(f"\nBuild this round's plan -- {names}?", default=True):
                stopped_reason = "human declined this round's build"
                break

        # Gate C: real pcp build, unmodified gate stack, real spend ceilings.
        from pcp.commands.build import build as build_cmd
        try:
            build_cmd.callback(module_name=module_name, project_path=str(project_root), yes=yes)
        except SystemExit as e:
            if e.code:
                console.print(f"[dim]diff-reduce: build step exited ({e.code}) this round[/dim]")

        # Gate D, end-of-round recheck.
        drift = _freshness_drifted(pcp_dir, stamp, module_names)
        if drift:
            escalations.record(
                pcp_dir, module_name or "_diff_reduce", f"round_{round_num}",
                route="diff-reduce-drift",
                findings=[f"diff-reduce round {round_num} aborted: {drift}. Not continuing against a target that moved."],
            )
            stopped_reason = f"aborted -- {drift}"
            break
    else:
        stopped_reason = f"round cap ({max_rounds}) reached"

    final_gap = _compute_gap(pcp_dir, module_name)
    gap_still_open = bool(final_gap["pending_count"] or final_gap["coverage_gaps"] or final_gap["missing_modules"])

    if stopped_reason == f"round cap ({max_rounds}) reached" and gap_still_open:
        escalations.record(
            pcp_dir, module_name or "_diff_reduce", "round_cap",
            route="diff-reduce-cap-hit",
            findings=[
                f"pcp diff-reduce used all {max_rounds} round(s) and the gap is still open "
                f"({final_gap['pending_count']} pending criteria, "
                f"{len(final_gap['coverage_gaps'])} coverage gap(s)) -- stopping is silent "
                "otherwise. Re-run diff-reduce, or investigate why it isn't converging."
            ],
        )

    console.print(f"\n[bold]diff-reduce finished:[/bold] {rounds_run} round(s) run, stopped because: {stopped_reason}")
    return {
        "rounds_run": rounds_run, "stopped_reason": stopped_reason,
        "gap_still_open": gap_still_open, "final_gap": final_gap,
    }


@click.command(name="diff-reduce")
@click.option("--module", "module_name", default=None, help="Scope to one module only.")
@click.option("--yes", "yes", is_flag=True,
              help="Skip diff-reduce's own per-round build confirm (CI use). Never bypasses "
                   "pcp pm's own confirm for new-scope criteria -- that gate is unconditional.")
@click.option("--max-rounds", "max_rounds", type=int, default=DEFAULT_MAX_ROUNDS,
              help=f"Round cap, default {DEFAULT_MAX_ROUNDS} (PCP's existing 3-attempt convention).")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
def diff_reduce(module_name: str | None, yes: bool, max_rounds: int, project_path: str | None):
    """Loop 2 -- read the build capsule, close the gap between spec and
    built state, bounded and gated. See this module's docstring for the
    five gates and why each one reuses an existing PCP mechanism."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    project_root = pcp_dir.parent
    result = run_diff_reduce(pcp_dir, project_root, module_name=module_name, yes=yes, max_rounds=max_rounds)
    sys.exit(1 if result["gap_still_open"] else 0)
