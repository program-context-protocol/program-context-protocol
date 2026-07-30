"""pcp build — autonomous agent execution loop to implement pending criteria."""

import json
import os
import re
import shutil
import sys
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import click
import yaml
from rich.console import Console

from pcp.pcp_dir import find_pcp_dir, get_modules_dir, NoPCPDir
from pcp.schema.validator import MalformedSpecError, validate_file, load_yaml
from pcp.llm import client as llm
from pcp.llm.client import _claude_bin, _log_usage
from pcp.pcp_status import write_pcp_md
from pcp import decision_log
from pcp import integrity_audit
from pcp import librarian
from pcp import narrative_lint
from pcp import objective_conflicts
from pcp import run_log
from pcp import assertions as assertions_lib
from pcp import telemetry
from pcp import qa
from pcp import evidence
from pcp import spend
from pcp import uat
from pcp.install_approvals import log_install_approval
from pcp.capture import find_transcript_for_session, run_capture

console = Console()

# Guards every write to a file shared across concurrent module builds
# (telemetry.jsonl, token_ledger.yaml, brd_items.yaml/decision_log.jsonl via
# pcp capture). Deliberately NOT held across gate evaluation itself (LLM
# calls, test/lint/SAST subprocess runs) — those are independent per module
# and are exactly the work parallelism exists to overlap. Only the brief
# read-modify-write file operations need to be serialized.
_STATE_LOCK = threading.Lock()


def _max_build_sessions() -> int:
    """Run-level circuit breaker on raw agent session spawns (sanity cap, not just per-criterion).

    Override with PCP_MAX_BUILD_SESSIONS for very large builds.
    """
    return int(os.environ.get("PCP_MAX_BUILD_SESSIONS", "150"))


def _build_agent_timeout_sec() -> int:
    """Wall-clock cap on a single coding-agent attempt. Found 2026-07-01: the
    subprocess.run() call for the coding agent had NO timeout at all — a stuck
    agent could run unbounded, and the session-count circuit breaker above
    can't help mid-session since it only checks before a NEW session starts.
    Override with PCP_BUILD_AGENT_TIMEOUT_SEC."""
    return int(os.environ.get("PCP_BUILD_AGENT_TIMEOUT_SEC", "1800"))


def _build_agent_max_budget_usd() -> str:
    """Per-attempt dollar cap passed to `claude -p --max-budget-usd`. Same gap
    as the timeout above, bounding runaway spend within one session rather
    than only across the whole run. Override with PCP_BUILD_AGENT_MAX_BUDGET_USD."""
    return os.environ.get("PCP_BUILD_AGENT_MAX_BUDGET_USD", "5")


def _max_agent_depth() -> int:
    """Hard cap on pcp build/pcp watch re-entrancy depth -- how many times a
    coding agent spawned by this process may itself trigger another pcp
    build/watch session before being refused. Mirrors Grok Build's
    depth-limit-1 subagent guard (see CLAUDE.md's Token Discipline section,
    2026-07-16 entry) but only covers PCP's own spawn points -- the
    `subprocess.run` calls to `claude -p` in build.py/watch.py. It does NOT
    and cannot enforce depth on Claude Code's own Agent/Workflow tools if a
    spawned agent calls those directly; that remains an instruction-level
    guard only (stated honestly in CLAUDE.md, not overclaimed as fixed here).
    Override with PCP_MAX_AGENT_DEPTH."""
    return int(os.environ.get("PCP_MAX_AGENT_DEPTH", "1"))


def check_agent_depth_or_exit() -> None:
    """Call once at the top of any command that spawns a coding-agent
    subprocess (pcp build, pcp watch's auto-fix loop). PCP_AGENT_DEPTH is
    set on this process's own environ after the check passes, which
    subprocess.run inherits automatically in every spawned `claude` child --
    the same zero-extra-plumbing mechanism PCP_AGENT_SESSION already uses
    below. If that child agent itself re-invokes `pcp build`/`pcp watch`,
    the nested call reads the inherited depth and is refused once the max
    is reached."""
    current_depth = int(os.environ.get("PCP_AGENT_DEPTH", "0"))
    max_depth = _max_agent_depth()
    if current_depth >= max_depth:
        console.print(
            f"[red bold]Subagent spawn-depth limit hit (depth={current_depth}, max={max_depth}).[/red bold]"
        )
        console.print(
            "[dim]A coding agent already running inside pcp build/watch attempted to spawn "
            "another pcp build/watch session -- refused. Override with PCP_MAX_AGENT_DEPTH=<n> "
            "only if you understand the runaway-recursion risk this guards against.[/dim]"
        )
        sys.exit(1)
    os.environ["PCP_AGENT_DEPTH"] = str(current_depth + 1)


def _max_parallel_modules() -> int:
    """Cap on concurrent module builds within one dependency wave, each in its
    own git worktree + branch — mirrors the /pcp orchestrator skill's module-
    level parallelism (criteria stay sequential within a module; modules in
    the same wave have no dependency on each other by construction, so the
    wave boundary is the only real gate). Default raised 3->5, 2026-07-20:
    a real-world swarm-role/parallelism research pass found 5-7 concurrent
    agents is the practical ceiling on a single machine before rate limits,
    merge conflicts, and review bottleneck erase the parallelism gain --
    3 was an arbitrary conservative guess, not measured against that ceiling.
    Kept below the top of that range since this is still the unattended CLI
    default, not an interactive session where a human is watching cost
    accrue in real time. Override with PCP_BUILD_MAX_PARALLEL."""
    return int(os.environ.get("PCP_BUILD_MAX_PARALLEL", "5"))


class BudgetExceeded(Exception):
    pass


class _BuildBudget:
    """Thread-safe session-count/cost tracking shared across module workers."""

    def __init__(self, max_sessions: int):
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.session_count = 0
        self.run_cost_total = 0.0
        self.tripped = False
        self.infra_signal_streak = 0
        self.infra_anomaly_tripped = False
        self.gate_skip_streaks: dict[str, int] = {}
        self.gate_skip_tripped: set[str] = set()

    def take_session(self) -> None:
        with self._lock:
            self.session_count += 1
            if self.session_count > self.max_sessions:
                self.tripped = True
                raise BudgetExceeded(self.session_count)

    def add_cost(self, cost: float | None) -> None:
        with self._lock:
            self.run_cost_total += cost or 0

    def record_test_timeout_signal(self, timed_out: bool) -> bool:
        """Cross-criterion anomaly signal. A real 2026-07-21 incident
        (ontology-foundry): a squatted DB port made the test-suite gate
        "time out" identically across several criteria before a human
        caught it -- per-criterion escalation (_record_escalation) only
        fires after a criterion exhausts all 3 attempts and never compares
        across criteria, so nothing flagged the repeating pattern itself.
        Returns True the moment PCP_BUILD_INFRA_ANOMALY_THRESHOLD consecutive
        timeout signals land, exactly once per run -- caller escalates loudly
        right then. Any non-timeout result resets the streak."""
        with self._lock:
            self.infra_signal_streak = self.infra_signal_streak + 1 if timed_out else 0
            threshold = int(os.environ.get("PCP_BUILD_INFRA_ANOMALY_THRESHOLD", "3"))
            if not self.infra_anomaly_tripped and self.infra_signal_streak >= threshold:
                self.infra_anomaly_tripped = True
                return True
            return False

    def record_gate_skip_signal(self, check: str, skipped: bool) -> bool:
        """Generalizes record_test_timeout_signal to any gate that can
        silently no-op when its underlying tool is present but broken
        (network fetch failure, a scan error, a crash) -- e.g. SAST after
        the 2026-07-21 fix now skips instead of blocking on a tool
        failure, the right default (don't false-block on infra), but it
        means a genuinely misconfigured tool could silently never gate
        anything again for the rest of an unattended multi-hour run unless
        something is watching for repeated skips. Deliberately does NOT
        fire for "tool not installed" (qa.py returns tool=None for that,
        never reaches here with skipped=True) -- that's expected, stable
        project config, not an anomaly. Tracked per-check (lint and sast
        streak independently) since one gate silently failing says nothing
        about the others. Fires once per check per run."""
        with self._lock:
            streak = self.gate_skip_streaks.get(check, 0)
            streak = streak + 1 if skipped else 0
            self.gate_skip_streaks[check] = streak
            threshold = int(os.environ.get("PCP_BUILD_GATE_SKIP_ANOMALY_THRESHOLD", "3"))
            if check not in self.gate_skip_tripped and streak >= threshold:
                self.gate_skip_tripped.add(check)
                return True
            return False


def _git_head(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=project_root,
    )
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def gather_modules_to_build(pcp_dir: Path, module_name: str | None = None) -> list[dict]:
    """Public — reused by external orchestrators (e.g. a multi-user/Temporal
    build layer) that want this run's exact module/criteria selection logic
    without duplicating it. Kept in sync with `build()`'s own gathering step
    by construction, since `build()` calls this too."""
    modules_dir = get_modules_dir(pcp_dir)
    modules_to_build = []
    for spec_path in sorted(modules_dir.glob("*/spec.yaml")):
        m_name = spec_path.parent.name
        if module_name and m_name != module_name:
            continue
        spec = load_yaml(spec_path)
        if spec.get("deprecated"):
            continue
        acc_path = spec_path.parent / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc_data = load_yaml(acc_path)
        pending = [c for c in acc_data.get("criteria", []) if c.get("status", "pending") == "pending"]
        if pending:
            modules_to_build.append({
                "name": m_name,
                "spec_path": spec_path,
                "acc_path": acc_path,
                "spec": spec,
                "pending_criteria": pending
            })
    return modules_to_build


def _compute_waves(modules_to_build: list[dict]) -> dict[str, int]:
    """{module_name: wave_number} via topological sort on each module's spec
    'dependencies' field. No in-set dependencies = wave 0. A module whose
    dependency isn't in this run's module set (already built, or external)
    is treated as satisfied — only in-set deps push it to a later wave."""
    name_to_mod = {m["name"]: m for m in modules_to_build}
    wave_of: dict[str, int] = {}

    def compute(name: str, seen: frozenset) -> int:
        if name in wave_of:
            return wave_of[name]
        if name in seen:
            return 0  # circular dependency — don't loop forever, treat as wave 0
        mod = name_to_mod.get(name)
        if not mod:
            return 0
        deps = [d for d in (mod["spec"].get("dependencies") or []) if d in name_to_mod and d != name]
        wave = 0 if not deps else 1 + max(compute(d, seen | {name}) for d in deps)
        wave_of[name] = wave
        return wave

    for m in modules_to_build:
        compute(m["name"], frozenset())
    return wave_of


# Public alias — external orchestrators reuse this alongside
# gather_modules_to_build() rather than reaching into a private name.
compute_waves = _compute_waves


# ── Criterion-level parallel waves within a single module ──────────────────
# Opt-in only. Grok Build's subagent model parallelizes at task granularity
# (not just top-level unit), worktree-isolated per task — reference-pattern
# borrowed 2026-07-16 rather than assuming criteria are safe to parallelize
# by default. Without any criterion in a module declaring `depends_on`, this
# is never consulted and the module's criteria build exactly as before:
# strictly sequential, each on the prior commit, in declared list order.

def _max_parallel_criteria() -> int:
    """Concurrency cap for criteria WITHIN one module.

    This pool was uncapped (`max_workers=len(wave_criteria)`) while the
    module-level pool was capped at 5 — the asymmetry behind the 2026-07-22
    30+-agent spawn, where the documented "15" was prose and nothing enforced
    it. Harmless while criterion parallelism was opt-in and almost nothing
    opted in; the moment it became the default, `core-data-model`'s 46
    independent criteria would have started 46 concurrent agents, each with a
    worktree and a test suite hitting the same Postgres.

    Defaults to 5, matching _max_parallel_modules(). Worst case is therefore
    modules x criteria concurrent agents, bounded overall by
    PCP_MAX_BUILD_SESSIONS. Raise PCP_BUILD_MAX_PARALLEL_CRITERIA for a
    single-module run (`--module X`), where no module-level fan-out is
    competing for the same database."""
    return max(1, int(os.environ.get("PCP_BUILD_MAX_PARALLEL_CRITERIA", "5")))


def _criteria_parallel_enabled(mod: dict) -> bool:
    """Criteria build in parallel by default.

    This used to require a module to "opt in" by having ANY criterion declare
    `depends_on`, even an empty list — presence as the signal. That reads
    exactly backwards: a module whose criteria declare NO dependencies is
    stating they are mutually independent, which is the *best* case for
    fanning out. PCP treated it as "not opted in" and ran the whole module one
    criterion at a time.

    Measured 2026-07-27, ontology-foundry: `logic-artifact-storage` has 12
    criteria and 0 declaring `depends_on`, so it ran a single agent
    sequentially. Across the project 145 of 382 criteria are in modules with
    no `depends_on` anywhere — all serial for want of a field whose absence
    already meant "independent".

    Parallelism is now the default and `depends_on` does the one job it should:
    ORDERING. Declared, it forces a criterion into a later wave; absent, the
    criterion is independent and lands in wave 0. The collision risk that once
    justified caution is handled where it belongs — optimistic scheduling with
    exact detection and a rebuild on conflict (see
    _partition_wave_by_file_scope and the merge path in _build_module_worker).

    PCP_CRITERIA_SERIAL=1 forces the old one-at-a-time behaviour."""
    if os.environ.get("PCP_CRITERIA_SERIAL") == "1":
        return False
    return len(mod["pending_criteria"]) > 1


def _compute_criterion_waves(mod: dict) -> dict[str, int]:
    """{criterion_id: wave_number} via topological sort on each pending
    criterion's `depends_on` field, mirroring _compute_waves()'s module-level
    logic one level down. A dependency on a criterion outside this run's
    pending set (already complete, or not yet declared) is treated as
    already satisfied — same "external dep = satisfied" rule as modules."""
    pending = {c["id"]: c for c in mod["pending_criteria"]}
    wave_of: dict[str, int] = {}

    def compute(cid: str, seen: frozenset) -> int:
        if cid in wave_of:
            return wave_of[cid]
        if cid in seen:
            return 0  # circular dependency — don't loop forever, treat as wave 0
        c = pending.get(cid)
        if not c:
            return 0
        deps = [d for d in (c.get("depends_on") or []) if d in pending and d != cid]
        wave = 0 if not deps else 1 + max(compute(d, seen | {cid}) for d in deps)
        wave_of[cid] = wave
        return wave

    for c in mod["pending_criteria"]:
        compute(c["id"], frozenset())
    return wave_of


def _partition_wave_by_file_scope(wave_criteria: list[dict]) -> list[list[dict]]:
    """Split one dependency wave into sub-waves that cannot collide on a file.

    `depends_on` expresses ORDER, never file disjointness. Two criteria with no
    dependency between them are scheduled together and each builds in its own
    worktree, blind to the other — so if both create the same file, both pass
    their own gates and the second merge dies on CONFLICT (add/add), leaving the
    build stopped and a human holding a git conflict.

    Observed 2026-07-27 (signtool dogfood): pdf-document-storage A001 and A004
    both created `src/pdf_document_storage/logging_safety.py` and both edited
    `pyproject.toml`. A004 merged; A001 could not. Flagged as a known risk on
    07-25 and left unfixed — this is that fix.

    The rule is conservative on purpose: run two criteria together only when
    PCP can PROVE they touch different files, which means both declared a
    `target` and the targets differ. A criterion with no declared target has an
    unknown file surface, so it gets a sub-wave to itself. That is the honest
    reading — fanning out work whose blast radius nobody declared is the unsafe
    act, not parallelism as such.

    Cost is real: `pcp kickoff` did not populate `target` at all until this same
    change taught it to, so existing projects lose criterion-level parallelism
    until their specs declare targets. That is the correct trade — a halted
    build and manual git surgery cost far more than serial execution, and the
    slowdown is exactly the incentive to declare targets. Set
    PCP_CRITERIA_PARALLEL_UNDECLARED=1 to restore the old optimistic behavior.

    Order within the wave is preserved; this only decides what may run
    alongside what.
    """
    # OPTIMISTIC by default (corrected 2026-07-27, same day it shipped
    # pessimistic). The first version ran two criteria together only when both
    # declared a `target` and the targets differed -- "prove disjointness or
    # run alone". On ontology-foundry that serialised 237 criteria that had
    # explicitly opted into parallel builds via depends_on, because only 51 of
    # 382 declare a target at all. A 15x throughput loss to prevent a collision
    # class that had bitten once.
    #
    # The trade was wrong because the collision is RECOVERABLE and cheap:
    # `_merge_module_branch` aborts cleanly (2026-07-25 fix), so a colliding
    # criterion costs one rebuild, while blanket serialisation costs a
    # multiple on every criterion in the project. Optimistic concurrency with
    # conflict-triggered retry beats pessimistic locking whenever conflicts
    # are rare and detection is exact -- and git merge is exact.
    #
    # Declared targets still buy something: two criteria that BOTH declare the
    # SAME target are known to collide before either runs, so they are still
    # separated up front rather than discovered at merge time.
    # PCP_CRITERIA_PARALLEL_STRICT=1 restores prove-or-serialise.
    strict = os.environ.get("PCP_CRITERIA_PARALLEL_STRICT") == "1"
    optimistic = not strict
    sub_waves: list[list[dict]] = []
    current: list[dict] = []
    claimed: set[str] = set()

    for c in wave_criteria:
        target = (c.get("target") or "").strip()
        if not target and not optimistic:
            # Unknown blast radius — never run alongside anything else.
            if current:
                sub_waves.append(current)
                current, claimed = [], set()
            sub_waves.append([c])
            continue
        key = target or f"__undeclared__{c['id']}"
        if key in claimed:
            sub_waves.append(current)
            current, claimed = [], set()
        current.append(c)
        claimed.add(key)

    if current:
        sub_waves.append(current)
    return sub_waves


# ── Worktree isolation for parallel module builds ──────────────────────────
# Each module being built concurrently gets its own git worktree + branch, same
# pattern as the /pcp skill's Branch Isolation Protocol. Only the coding
# agent's source-code edits go through the worktree/branch/merge path — all
# .pcp/ state (telemetry, token_ledger, this module's own acceptance.yaml)
# is written directly by this Python process to the MAIN pcp_dir, guarded by
# a lock, regardless of which worktree the agent subprocess ran in. That
# sidesteps git-merge-conflict risk on shared audit files entirely: they
# never diverge across branches because only one process ever writes them.

def _worktree_dir(project_root: Path, module_name: str) -> Path:
    return project_root.parent / f"{project_root.name}-{module_name}"


def _sync_worktree_to_base(wt_path: Path, base_sha: str, module_name: str) -> None:
    """Bring a REUSED branch/worktree up to the base commit before building on
    it. A fresh `-b` branch already starts at the base; a branch left over from
    an earlier run (or from an earlier wave whose siblings have since merged)
    can be arbitrarily far behind, and every commit main gained in the meantime
    becomes conflict surface at merge time. Merging base in here moves that
    reconciliation to the START of the criterion, where the agent still has a
    session to fix it, instead of the end, where `_merge_module_branch` can only
    report the conflict and leave the worktree behind.

    Deliberately NOT a rebase: rebasing rewrites commits an interrupted run may
    already have pushed. No-op when already up to date. On conflict or a dirty
    tree, abort and warn rather than hand the agent a half-merged checkout."""
    if not base_sha:
        return
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wt_path, capture_output=True, text=True,
    ).stdout.strip()
    if dirty:
        console.print(
            f"[yellow]Worktree for '{module_name}' has uncommitted changes from a prior run — "
            f"skipping base sync. It may be behind main.[/yellow]"
        )
        return
    result = subprocess.run(
        ["git", "merge", "--no-edit", base_sha], cwd=wt_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=wt_path, capture_output=True)
        console.print(
            f"[yellow]Could not sync worktree for '{module_name}' to the current base "
            f"(conflict). Building on a stale base; expect a merge conflict at the end.[/yellow]"
        )


def _seed_testmon_cache(project_root: Path, wt_path: Path) -> None:
    """Copy the main checkout's `.testmondata` into a fresh worktree.

    `pytest-testmon` was adopted (2026-07-24) so a per-criterion QA gate runs
    only the tests a change actually affects. It delivered **zero** benefit
    inside `pcp build`, because its cache is gitignored and `git worktree add`
    does not carry gitignored files across -- verified empirically on
    ontology-foundry, where a 448K `.testmondata` sits in the main checkout and
    a fresh worktree has none.

    Cold testmon is not merely "no speedup", it is *slower than plain pytest*:
    with no prior coverage DB it must trace coverage across the whole scoped set
    to build one, then the worktree is deleted and the warm cache dies with it.
    So parallel builds paid tracing overhead on every criterion and never once
    collected the selection benefit.

    Seed-only, deliberately never copied back. N concurrent worktrees each write
    their own sqlite DB reflecting only the subset they ran; merging those back
    would either corrupt the selection state or overwrite a broader baseline
    with a narrower one. The main checkout's DB stays the single baseline, and
    each worktree starts from the same base_sha it was cut from, which is
    exactly the state that DB describes.

    Best-effort: any failure here leaves testmon cold, which is the current
    behaviour, so it must never break a build."""
    src = project_root / ".testmondata"
    dst = wt_path / ".testmondata"
    if not src.is_file() or dst.exists():
        return
    try:
        shutil.copy2(src, dst)
    except OSError:
        pass


def _setup_worktree(project_root: Path, module_name: str) -> Path:
    wt_path = _worktree_dir(project_root, module_name)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True,
    ).stdout.strip()
    if wt_path.exists():
        # Reuse from a prior interrupted run — but not its stale base.
        _sync_worktree_to_base(wt_path, base_sha, module_name)
        _seed_testmon_cache(project_root, wt_path)
        return wt_path
    branch = f"feat/{module_name}"
    branch_exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch], cwd=project_root, capture_output=True,
    ).returncode == 0
    cmd = ["git", "worktree", "add", str(wt_path)] + ([branch] if branch_exists else ["-b", branch])
    subprocess.run(cmd, cwd=project_root, capture_output=True, text=True)
    if branch_exists:
        _sync_worktree_to_base(wt_path, base_sha, module_name)
    _seed_testmon_cache(project_root, wt_path)
    return wt_path


def _merge_module_branch(project_root: Path, module_name: str, pcp_dir: Path | None = None) -> tuple[bool, str]:
    branch = f"feat/{module_name}"
    result = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}"],
        cwd=project_root, capture_output=True, text=True,
    )
    ok = result.returncode == 0
    if not ok:
        # Leave NO half-merged state behind. Without this, a conflicting merge
        # leaves project_root mid-MERGE with conflict markers in the tree, and
        # every subsequent git command in that repo fails on unmerged paths --
        # so one conflicted criterion takes down the whole run and everything
        # after it, including criteria that had already passed their gates.
        # That is what made 2026-07-25's `.claude/settings.json` add/add
        # conflict so destructive: that fix removed one CAUSE of a conflict,
        # this handles the CONSEQUENCE of any conflict at all. The caller
        # already treats `ok=False` as a failure and leaves the worktree up
        # for manual resolution -- aborting here only cleans the main repo,
        # it does not discard the branch or the agent's work.
        subprocess.run(["git", "merge", "--abort"], cwd=project_root, capture_output=True)
    # Conflict-rate telemetry (2026-07-17): AgenticFlict (arXiv:2604.03551)
    # measured a 27.67% merge-conflict baseline for agent-authored PRs; PCP's
    # worktree-isolated wave merges should beat that, and now the data to
    # prove/refute it accumulates — `pcp telemetry` reports the rate.
    if pcp_dir is not None:
        with _STATE_LOCK:
            telemetry.record(
                pcp_dir, cycle="qa", cycle_number=None, check="worktree-merge", control_id=None,
                module=module_name, submodule=None, criterion_id=None, files=[],
                result="pass" if ok else "block",
                errors=[] if ok else [(result.stdout + result.stderr)[-500:]],
                error_count=0 if ok else 1,
            )
    return ok, (result.stdout + result.stderr)


def _cleanup_worktree(project_root: Path, module_name: str, wt_path: Path) -> None:
    subprocess.run(["git", "worktree", "remove", str(wt_path), "--force"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "branch", "-D", f"feat/{module_name}"], cwd=project_root, capture_output=True)


def _auto_commit_criterion(project_root: Path, module_name: str, criterion: dict) -> None:
    """Safety net, not a replacement for the agent's own commit.
    `_build_agent_prompt` deliberately leaves committing optional for the
    agent (gates measure the working diff either way) — but Ganesh's global
    Build Cycle rule treats commit as part of the same cycle as build, not a
    separate step a human opts into later. Once a criterion has passed every
    gate, commit whatever is still sitting uncommitted so real work doesn't
    rot in a worktree if the run stops before the module finishes (the
    2026-07-23 ontology-foundry web-server worktrees). No-op if the agent
    already committed — working tree is already clean."""
    status = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, cwd=project_root,
    )
    if not status.stdout.strip():
        return
    # `git add -A` minus agent-session-local config. Claude Code writes
    # .claude/settings*.json into whatever directory it runs in, with values
    # scoped to THAT directory (TMPDIR, granted permissions) -- so every
    # parallel worktree produces a different version of the same new path.
    # Committing them makes every wave merge an add/add conflict on a scratch
    # file (2026-07-25 ontology-foundry: three criteria that had passed all
    # their gates could not be merged). `pcp init`'s .gitignore covers new
    # projects; this covers every project that already had a .gitignore, which
    # init deliberately never modifies.
    subprocess.run(
        ["git", "add", "-A", "--", *_AUTO_COMMIT_EXCLUDES],
        cwd=project_root, capture_output=True,
    )
    message = f"{module_name}/{criterion['id']}: {criterion.get('description', '')}".strip()
    result = subprocess.run(
        ["git", "commit", "-m", message], cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        console.print(f"[yellow]Auto-commit for {module_name}/{criterion['id']} failed:[/yellow] {result.stderr.strip()[:300]}")


def _auto_push(project_root: Path) -> None:
    """Step 3 of the global Build Cycle — push if a remote is configured,
    skip silently otherwise (matches the rule's own "if the repo has a
    remote configured" condition). Never force-pushes; a rejected push
    (non-fast-forward, no upstream) is reported, not retried or escalated —
    that's a real divergence a human should look at, not paper over."""
    remote = subprocess.run(["git", "remote"], capture_output=True, text=True, cwd=project_root)
    if not remote.stdout.strip():
        return
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=project_root,
    ).stdout.strip()
    if not branch or branch == "HEAD":
        return
    result = subprocess.run(
        ["git", "push", "origin", branch], cwd=project_root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        console.print(f"[dim]Pushed {branch} to origin.[/dim]")
    else:
        console.print(f"[yellow]Auto-push of {branch} failed — resolve manually:[/yellow] {result.stderr.strip()[:300]}")


def _wave_record(pcp_dir: Path, wave_number: int, check: str, control_id: str, errors: list[str],
                  files: list[str] | None = None, result: str | None = None,
                  evidence_path: str | None = None) -> None:
    """Wave-merge gates have no single criterion_id/attempt — record at cycle_number=wave_number
    so they still land in the same telemetry.jsonl audit trail as per-criterion QA checks,
    instead of only ever reaching the user as a console line."""
    if result is None:
        result = "block" if errors else "pass"
    elif result == "pass" and errors:
        # An advisory check passes `result="pass"` explicitly to say "found
        # things, but do not block the wave". Recording that as a literal
        # "pass" made the audit trail lie: eleven controls (CTRL-008, 019,
        # 020, 021, 025, 027, 028, 030, 031, 033, 036) reported a clean pass
        # in telemetry.jsonl no matter what they found, and `pcp provenance`
        # reads exactly that field. A tool selling audit-grade evidence must
        # not have its own controls falsify it.
        #
        # "advisory" is the honest third value: the check ran, it found
        # something, and it deliberately did not block. Distinct from "pass"
        # (found nothing), "block" (found something and stopped the wave),
        # and "skipped" (never ran at all). `error_count` was always correct
        # here, which is why `pcp control-audit` — keying off error_count —
        # was unaffected; provenance keys off `result` and was not.
        result = "advisory"
    telemetry.record(
        pcp_dir,
        cycle="qa", cycle_number=wave_number, check=f"wave-{check}", control_id=control_id,
        module=None, submodule=None, criterion_id=None,
        files=files or [], result=result, errors=errors, error_count=len(errors),
        evidence_path=evidence_path,
    )


def _write_progress(pcp_dir: Path, module: str, criterion_id: str, attempt: int, step: str) -> None:
    """Live build progress (2026-07-24) -- .pcp/build_progress.yaml, read by
    `pcp build-status`. A backgrounded/parallel-worktree build with no way
    to see what's currently running is exactly what triggered a real
    ontology-foundry incident (07-21: 'i want to see whats happening').
    Advisory/UX only -- a write failure here must never fail a real build."""
    try:
        from datetime import datetime, timezone
        data = {
            "module": module, "criterion_id": criterion_id, "attempt": attempt, "step": step,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (pcp_dir / "build_progress.yaml").write_text(yaml.dump(data, default_flow_style=False))
    except Exception:
        pass


_DEP_IN_FINDING = re.compile(r"depends on '([^']+)'")


def _finding_blames_outside_wave(finding: str, wave_mod_names: set[str]) -> bool:
    """Is this finding a statement about a module the wave did not build?

    CTRL-007 (see _run_wave_merge step 1) fires when a wave module's declared
    dependency has incomplete criteria. That is a fact about the DEPENDENCY, and
    the dependency is routinely not in this wave at all -- waves exist precisely
    to build dependencies first. Nothing the wave's own agents wrote caused it
    and nothing they could write would fix it.

    Deterministic, rung 1: read the dependency name out of the finding (producer
    and consumer are both in this file, so the format is not a guess) and ask
    whether it was in the wave. No LLM, no `target` field required -- which is
    what made per-criterion attribution impractical."""
    m = _DEP_IN_FINDING.search(finding)
    return bool(m) and m.group(1) not in wave_mod_names


def _reopen_wave_criteria(pcp_dir: Path, wave_modules: list[dict], wave_number: int,
                          findings: list[str]) -> None:
    """Un-complete the criteria a blocking wave gate just judged defective.

    Reopens everything completed in the wave rather than guessing WHICH criterion
    caused it: per-criterion attribution would need each criterion's declared
    `target`, which real projects overwhelmingly do not populate (51 of 382 on
    ontology-foundry). Reopening too much costs a rebuild; reopening too little
    leaves a vulnerability marked verified. Those failure directions are not
    symmetric, so the coarse choice is right.

    But "which criterion" and "was this the wave's work at all" are different
    questions, and only the first one needs `target`. Reopening on a finding that
    is purely about pre-existing state elsewhere is not conservative, it is a
    deadlock: the wave gate requires dependencies 100% complete, so a module
    downstream of an incomplete dependency can never pass, and every attempt
    reverts work that was merged and correct.

    Measured twice on ontology-foundry. A036-A039 (agent-query-interface) were
    reverted on five blockers, none from that build. Then on 2026-07-30,
    core-data-model A022/A030/A033/A038 -- **$30.04 spent, all four branches
    merged into main, all four marked `pending`**. They were the four most
    expensive criteria in the run and the four with nothing to show for it.

    So: if EVERY finding is about a module outside the wave, the wave's own work
    is not implicated and nothing is reopened. The gate still blocks, and the
    escalation is still recorded either way -- the finding is real and forward
    progress still stops. Only the false claim "this criterion is not built" is
    withdrawn.

    Also records one escalation per module so the finding outlives the console
    line that reported it -- `pcp escalations` can show it, and it is no longer
    possible for a wave BLOCK to leave zero trace in `.pcp/`."""
    from pcp import escalations

    wave_mod_names = {m["name"] for m in wave_modules}
    external = [f for f in findings if _finding_blames_outside_wave(f, wave_mod_names)]
    attributable = [f for f in findings if f not in external]
    if findings and not attributable:
        console.print(
            f"[yellow]Wave {wave_number} blocked by {len(external)} finding(s) about "
            "module(s) outside this wave — criteria NOT reopened, because this wave's "
            "own work is not what the gate objected to.[/yellow]"
        )
        for f in external:
            console.print(f"[dim]   external: {f}[/dim]")
        console.print(
            "[dim]The block stands and an escalation is recorded. Build the named "
            "dependency first; these criteria stay complete because they are.[/dim]"
        )
        for mod in wave_modules:
            with _STATE_LOCK:
                escalations.record(
                    pcp_dir, mod["name"], f"wave_{wave_number}", route="wave-block",
                    findings=findings,
                )
        return

    reopened: list[str] = []
    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        built_ids = {c["id"] for c in mod.get("pending_criteria", [])}
        if not built_ids:
            continue
        try:
            with _STATE_LOCK:
                acc = load_yaml(acc_path) or {}
                changed = False
                for c in acc.get("criteria", []):
                    if c.get("id") in built_ids and c.get("status") == "complete":
                        c["status"] = "pending"
                        c.pop("verified_by", None)
                        reopened.append(f"{mod['name']}/{c['id']}")
                        changed = True
                if changed:
                    acc_path.write_text(yaml.dump(acc, default_flow_style=False))
        except MalformedSpecError as exc:
            console.print(f"[yellow]Could not reopen '{mod['name']}' criteria: {exc}[/yellow]")
            continue
        with _STATE_LOCK:
            escalations.record(
                pcp_dir, mod["name"], f"wave_{wave_number}", route="wave-block",
                findings=findings,
            )

    if reopened:
        console.print(
            f"[yellow]Reopened {len(reopened)} criteria judged defective by this wave "
            f"(status complete -> pending): {', '.join(reopened[:8])}"
            f"{'...' if len(reopened) > 8 else ''}[/yellow]"
        )
        console.print(
            "[dim]The code stays merged; the criteria no longer claim to be verified. "
            "The next `pcp build` rebuilds them with these findings as feedback.[/dim]"
        )


def _run_wave_merge(pcp_dir: Path, wave_modules: list[dict], wave_start_ref: str, wave_number: int = 0) -> list[str]:
    """Per docs/greenfield.md Phase 4 — contract validation, full integration
    test suite, validate-strategy re-check, wave-level architect-review."""
    project_root = pcp_dir.parent
    findings: list[str] = []
    wave_mod_names = [m["name"] for m in wave_modules]

    # 1. Contract validation — declared dependencies must be fully complete, not half-built.
    contract_findings: list[str] = []
    for mod in wave_modules:
        for dep in (mod["spec"].get("dependencies") or []):
            dep_acc_path = pcp_dir / "strategy" / "modules" / dep / "acceptance.yaml"
            if not dep_acc_path.exists():
                contract_findings.append(f"Contract: '{mod['name']}' depends on '{dep}', which has no acceptance.yaml")
                continue
            dep_acc = load_yaml(dep_acc_path)
            incomplete = [c["id"] for c in dep_acc.get("criteria", []) if c.get("status", "pending") != "complete"]
            if incomplete:
                contract_findings.append(
                    f"Contract: '{mod['name']}' depends on '{dep}', which has incomplete criteria: {', '.join(incomplete)}"
                )
    _wave_record(pcp_dir, wave_number, "contract", "CTRL-007", contract_findings, files=wave_mod_names)
    findings += contract_findings

    # 2. Full integration test suite on the merged state.
    test_result = qa.run_test_suite(project_root)
    test_findings: list[str] = []
    wave_evidence_path = None
    if test_result["tool"]:
        wave_evidence_path = evidence.store(
            pcp_dir, "_wave", f"wave_{wave_number}", wave_number, "test-suite", test_result["output"],
        )
    if test_result["tool"] and not test_result["passed"]:
        test_findings.append(f"Wave integration suite ({test_result['tool']}) FAILED — full output: {wave_evidence_path}\n{test_result['output'][-1500:]}")
    _wave_record(pcp_dir, wave_number, "test-suite", "CTRL-001", test_findings, files=wave_mod_names,
                 result="skipped" if not test_result["tool"] else None, evidence_path=wave_evidence_path)
    findings += test_findings

    # 3. validate-strategy re-check — coverage/coupling after this wave's changes.
    try:
        from pcp.commands.validate_strategy import run_validate_strategy
        vs = run_validate_strategy(pcp_dir, command="wave-validate-strategy")
        strategy_findings: list[str] = []
        advisory_recorded = False
        vs_evidence_path = evidence.store(
            pcp_dir, "_wave", f"wave_{wave_number}", wave_number, "validate-strategy",
            json.dumps(vs, indent=2, default=str) if vs else "(no result)",
        )
        if vs:
            severe_coupling = [v for v in vs.get("coupling_violations", []) if v.get("type") in ("circular", "god_module", "shared_state")]
            coverage_gaps = vs.get("coverage_gaps") or []

            # Scorer-consensus rule (dogfood round 3, 2026-07-17): the
            # deterministic assertion scorer's own docstring calls keyword
            # overlap "not ground truth" — a rung-1 heuristic with known
            # false negatives (real coverage, different words). It scored
            # 50% against the LLM's 100% on UNCHANGED specs and hard-blocked
            # the wave. Two scorers disagreeing is an uncertainty signal,
            # not a verdict (the ensemble/consensus mechanism from the
            # Logic-Tier cross-cutting table) — coverage hard-blocks only
            # when both agree it's bad. Severe coupling always blocks: that
            # is real graph math, no second opinion needed.
            llm_score = vs.get("llm_coverage_score")
            # Shared with `pcp validate-strategy` (assertions.scorers_disagree)
            # so the two can never drift into disagreeing about the same data.
            # credibility_floor=1.0: any disagreement is advisory here. The
            # wave gate re-checks UNCHANGED specs after every wave, so a dip
            # is noise rather than new evidence — unlike a standalone audit.
            disagree = assertions_lib.scorers_disagree(vs, credibility_floor=1.0)
            if coverage_gaps and disagree and not severe_coupling:
                console.print(
                    f"[yellow]Wave validate-strategy (advisory): deterministic assertion "
                    f"coverage {vs.get('coverage_score', 0):.0%} disagrees with LLM coverage "
                    f"{llm_score:.0%} on unchanged-spec gaps ({len(coverage_gaps)}) — treating as "
                    f"scorer disagreement, not blocking. Full result: {vs_evidence_path}[/yellow]"
                )
                _wave_record(
                    pcp_dir, wave_number, "validate-strategy", "CTRL-008",
                    [f"scorer disagreement (advisory): deterministic={vs.get('coverage_score', 0):.0%} "
                     f"vs llm={llm_score:.0%}, gaps={len(coverage_gaps)}"],
                    files=wave_mod_names, result="pass", evidence_path=vs_evidence_path,
                )
                advisory_recorded = True  # telemetry written above; not blocking
            elif coverage_gaps or severe_coupling:
                strategy_findings.append(
                    f"validate-strategy: coverage={vs.get('coverage_score', 0):.0%}, "
                    f"coupling={vs.get('coupling_score', 1):.0%}, "
                    f"gaps={len(coverage_gaps)}, "
                    f"severe coupling violations={len(severe_coupling)} (circular/god_module/shared_state) — "
                    f"full result: {vs_evidence_path}"
                )
        if not advisory_recorded:
            _wave_record(pcp_dir, wave_number, "validate-strategy", "CTRL-008", strategy_findings, files=wave_mod_names,
                         evidence_path=vs_evidence_path)
            findings += strategy_findings
    except Exception as e:
        console.print(f"[yellow]Warning: wave validate-strategy check failed: {e}[/yellow]")
        _wave_record(pcp_dir, wave_number, "validate-strategy", "CTRL-008", [f"call failed: {e}"],
                     files=wave_mod_names, result="error")

    # 3.5. Per-module spec alignment (Two Validation Passes, Pass 1) — does
    # each module in this wave still align with the objective/decomposition?
    # Distinct from step 3's validate-strategy (Pass 2: do modules
    # collectively cover the objective) -- this checks each module's own
    # spec individually. Advisory: false-positive rate not measured yet.
    module_align_findings: list[str] = []
    try:
        from pcp.commands.validate_module import run_validate_module
        for mod in wave_modules:
            mod_name = mod["name"]
            result = run_validate_module(pcp_dir, mod_name)
            if result is None:
                continue
            mod_evidence_path = evidence.store(
                pcp_dir, "_wave", f"wave_{wave_number}", wave_number, f"validate-module-{mod_name}",
                json.dumps(result, indent=2, default=str),
            )
            if not result.get("aligned", True):
                console.print(
                    f"[yellow]Wave validate-module (advisory): '{mod_name}' alignment "
                    f"{result.get('alignment_score', 0):.0%} — full result: {mod_evidence_path}[/yellow]"
                )
        _wave_record(pcp_dir, wave_number, "validate-module", "CTRL-024", [], files=wave_mod_names, result="pass")
    except Exception as e:
        console.print(f"[yellow]Warning: wave validate-module check failed: {e}[/yellow]")
        _wave_record(pcp_dir, wave_number, "validate-module", "CTRL-024", [f"call failed: {e}"],
                     files=wave_mod_names, result="error")

    # 4. Wave-level architect-review — diff since the wave started, not just the last criterion.
    try:
        from pcp.commands.architect_review import (
            SYSTEM_PROMPT as ARCH_SYSTEM_PROMPT, _build_prompt as _arch_build_prompt,
            _load_persona, _load_kb, _get_diff, _changed_files_from_diff,
        )
        wave_diff = _get_diff(wave_start_ref)
        arch_findings: list[str] = []
        if wave_diff.strip():
            changed = _changed_files_from_diff(wave_diff)
            persona = _load_persona(pcp_dir)
            architecture = (pcp_dir / "architecture.md").read_text() if (pcp_dir / "architecture.md").exists() else ""
            kb = _load_kb(pcp_dir, changed)
            prompt = _arch_build_prompt(persona, architecture, kb, wave_diff, "diff")
            # Opus, not Haiku -- a wave-level BLOCK finding stops the entire
            # next wave, a materially higher blast radius than a per-
            # criterion check (see llm/client.py's model-selection strategy).
            res = llm.call_json(ARCH_SYSTEM_PROMPT, prompt, model=llm.ESCALATION_MODEL, pcp_dir=pcp_dir, command="wave-architect-review")
            for f in res.get("findings", []):
                if f.get("severity") == "BLOCK":
                    arch_findings.append(f"Wave architect-review: {f.get('location', 'general')}: {f.get('finding', '')} → Fix: {f.get('fix', '')}")
            arch_evidence_path = evidence.store(
                pcp_dir, "_wave", f"wave_{wave_number}", wave_number, "architect-review", json.dumps(res, indent=2),
            )
            # Same adversarial re-verification per-criterion architect-review/gate
            # checks already get (_verify_block_findings) -- wave-level BLOCK
            # findings previously went straight from one Haiku call into a
            # blocked wave-merge with no second opinion, unlike their
            # per-criterion counterparts. wave_ctx mirrors the per-criterion
            # ctx shape (_qa_record/evidence.store both key off module/
            # criterion_id/attempt) with module="_wave" so verify-check
            # telemetry is distinguishable from real per-criterion records.
            wave_ctx = {"module": "_wave", "criterion_id": f"wave_{wave_number}", "attempt": wave_number, "files": changed}
            arch_findings, _dropped = _verify_block_findings(
                pcp_dir, wave_diff, arch_findings, wave_ctx, "wave-architect-review", "CTRL-005",
            )
            _wave_record(pcp_dir, wave_number, "architect-review", "CTRL-005", arch_findings, files=changed,
                         evidence_path=arch_evidence_path)
        findings += arch_findings
    except Exception as e:
        console.print(f"[yellow]Warning: wave architect-review failed: {e}[/yellow]")
        _wave_record(pcp_dir, wave_number, "architect-review", "CTRL-005", [f"call failed: {e}"],
                     files=wave_mod_names, result="error")

    # 5. logic_tier drift -- does what actually got built still match the
    #    tier a criterion declared at spec time?
    tier_findings = _run_wave_tier_drift_check(pcp_dir, wave_modules, wave_number)
    findings += tier_findings

    # 6. build_vs_buy drift -- narrower scope than tier drift, see the
    #    function's own docstring for why only reuse_whole/fork_adapt get
    #    checked, not build_fresh.
    bvb_findings = _run_wave_build_vs_buy_drift_check(pcp_dir, wave_modules, wave_number)
    findings += bvb_findings

    # 7-8. Logic-tier integrity, ADVISORY pair (2026-07-18): positive
    # mechanism-presence check for rungs 2-5 (CTRL-019) and rung-necessity
    # challenge (CTRL-020). Both record + print, neither blocks — presence
    # has a known false-positive path (mechanism can live in an imported
    # helper, not the target file itself), and necessity is a semantic
    # judgment; per the L1-report-first standing rule both earn hard-block
    # status only after a measured false-positive rate says they deserve it.
    _run_wave_tier_presence_check(pcp_dir, wave_modules, wave_number)
    _run_wave_rung_necessity_check(pcp_dir, wave_modules, wave_number)

    # 9. Context-route staleness (CTRL-021, advisory) — the routing table is
    # itself a drift surface; a stale route starves agents silently.
    from pcp import context_map
    route_findings = context_map.validate(pcp_dir)
    _wave_record(pcp_dir, wave_number, "context-routes", "CTRL-021", route_findings,
                 files=[], result="pass")
    for f in route_findings:
        console.print(f"[yellow]{f}[/yellow]")

    # 10. Navigation depth outliers (CTRL-025, advisory) and 11. top-menu-bar
    # convention (CTRL-027, advisory, desktop_app archetype only) — neither
    # blocks, same "report first, measure false-positive rate" posture as
    # tier-presence/rung-necessity above.
    _run_wave_nav_depth_check(pcp_dir, wave_modules, wave_number)
    _run_wave_menu_bar_check(pcp_dir, wave_modules, wave_number)

    # 12. UI kit recipe completeness + import verification (CTRL-028,
    # advisory) — inert unless .pcp/ui_kit_recipes.yaml exists.
    _run_wave_ui_kit_check(pcp_dir, wave_modules, wave_number)

    # 13. module_logic_breakdown built-code verification (CTRL-031,
    # advisory) — inert unless a module declares module_logic_breakdown.
    _run_wave_logic_breakdown_check(pcp_dir, wave_modules, wave_number)

    # 13.5. ci_rules.yaml contract completeness (CTRL-033, advisory,
    # project-wide, not per-module).
    _run_wave_contract_completeness_check(pcp_dir, wave_number)

    # 13.6. Narrative lint (CTRL-036, advisory, project-wide) — CLAUDE.md-
    # family narrative prose vs. tracked state (current_state.md/
    # architecture.md). Costs one Haiku call only when a status-shaped line
    # exists to check.
    _run_wave_narrative_lint_check(pcp_dir, wave_number)

    # 14. Integrity Auditor (CTRL-030, advisory) — retrospective statistical-
    # drift signals across ALL completed criteria so far: fast completions
    # vs. declared logic_tier, per-module placeholder-flag concentration,
    # findings recurring across many criteria without resolving, uniform/
    # templated evidence. Reads only; can't correct what's already built —
    # flags for human review, same posture escalations.yaml already has.
    # Runs at the wave boundary, not per-criterion — the value is seeing
    # patterns across many completed criteria no single-criterion CTRL
    # check can see by design.
    integrity_findings = integrity_audit.analyze(pcp_dir)
    _wave_record(pcp_dir, wave_number, "integrity-audit", "CTRL-030", integrity_findings,
                 files=[], result="pass")
    for f in integrity_findings:
        console.print(f"[yellow]Integrity Auditor (advisory):[/yellow] {f}")

    return findings


LLM_SDK_IMPORT_PATTERN = re.compile(
    r"^\s*(?:import|from)\s+(anthropic|openai|google\.generativeai|google\.genai|mistralai|cohere|ollama)\b",
    re.MULTILINE,
)


def _run_wave_tier_drift_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """5th wave-merge sub-check, CTRL-014. Per CLAUDE.md's Logic-Tier
    Selection section: "Layer 1 gets a deterministic tier-honesty sub-check
    where possible (e.g. a criterion declaring logic_tier <= 5 whose target
    file imports an LLM SDK)" -- not built until now. Deterministic, no LLM
    call: a completed criterion declaring logic_tier 1-5 (deterministic
    through cached-reuse -- no runtime LLM call expected by definition) whose
    own target file demonstrably imports an LLM SDK is a real signal the
    declared decision no longer matches what was actually built. Only rung 6
    (deep-think LLM) is expected to import one. build_vs_buy gets its own,
    narrower, separate check -- _run_wave_build_vs_buy_drift_check below."""
    project_root = pcp_dir.parent
    findings: list[str] = []
    checked_files: list[str] = []

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete":
                continue
            tier = c.get("logic_tier")
            target = c.get("target")
            if tier is None or tier > 5 or not target:
                continue
            full_path = project_root / target
            if not full_path.exists() or not full_path.is_file():
                continue
            checked_files.append(target)
            try:
                content = full_path.read_text(errors="replace")
            except OSError:
                continue
            m = LLM_SDK_IMPORT_PATTERN.search(content)
            if m:
                findings.append(
                    f"Tier drift: '{mod['name']}/{c['id']}' declares logic_tier={tier} "
                    f"(rung <=5, no runtime LLM call expected) but {target} imports "
                    f"{m.group(1)} -- the declared decision no longer matches what was built."
                )

    _wave_record(pcp_dir, wave_number, "tier-drift", "CTRL-014", findings, files=checked_files)
    return findings


def _stdlib_module_names() -> frozenset[str]:
    import sys
    names = getattr(sys, "stdlib_module_names", None)
    return frozenset(names) if names else frozenset()


def _local_package_names(project_root: Path) -> frozenset[str]:
    """Top-level directory names under src/ (or the project root itself if
    no src/ layout) -- a Python `import` of one of these is a local project
    import, not an external dependency."""
    src = project_root / "src"
    base = src if src.exists() else project_root
    return frozenset(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def _external_python_imports(target_path: Path, project_root: Path) -> set[str]:
    """Top-level import names from a Python file, excluding stdlib and this
    project's own local packages -- what's left is a real external/
    third-party dependency. Python-only: generalizing import extraction
    across every EXTRACTORS language (discovery/graph.py) for one heuristic
    check isn't worth the added surface for this pass."""
    if target_path.suffix != ".py":
        return set()
    from pcp.discovery.graph import extract_imports_python
    raw = extract_imports_python(target_path, project_root)
    return {i for i in raw if i not in _stdlib_module_names() and i not in _local_package_names(project_root)}


def _run_wave_build_vs_buy_drift_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """6th wave-merge sub-check, CTRL-016. Same "does the declared decision
    still match what got built" question _run_wave_tier_drift_check asks of
    logic_tier, applied to build_vs_buy -- but deliberately narrower scope:
    only `reuse_whole`/`fork_adapt` get checked (declaring one of these
    means an external dependency SHOULD be there -- a target file with zero
    external imports despite that claim is a real, cheap, low-false-positive
    signal). `build_fresh` is NOT checked in the other direction ("does it
    import something new"): package names routinely differ from their
    import names (pyyaml->yaml, beautifulsoup4->bs4, pillow->PIL), which
    would make a "no new external import" check noisy enough to be
    untrustworthy as a hard_block gate. `reuse_partial`/
    `reimplement_from_reference` are skipped entirely -- vendored or
    reimplemented code has no distinguishing import signature either way.
    Left for a future pass rather than shipping something that guesses."""
    project_root = pcp_dir.parent
    findings: list[str] = []
    checked_files: list[str] = []

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete":
                continue
            decision = (c.get("build_vs_buy") or {}).get("decision")
            target = c.get("target")
            if decision not in ("reuse_whole", "fork_adapt") or not target:
                continue
            full_path = project_root / target
            if not full_path.exists() or not full_path.is_file():
                continue
            checked_files.append(target)
            externals = _external_python_imports(full_path, project_root)
            if not externals:
                findings.append(
                    f"Build-vs-buy drift: '{mod['name']}/{c['id']}' declares "
                    f"build_vs_buy={decision} but {target} imports no external package -- "
                    f"the declared decision no longer matches what was built."
                )

    _wave_record(pcp_dir, wave_number, "build-vs-buy-drift", "CTRL-016", findings, files=checked_files)
    return findings


# PCP's own operational writes during a build attempt (usage logging, telemetry,
# evidence, capture). Found dogfooding 2026-07-17: every LLM call appends to the
# project's .pcp/token_ledger.yaml, which then landed in changed_files and the
# judge diff — attempt 1's alignment gate literally scored the token ledger as
# the PR ("Score 0%: token ledger entry; no implementation progress") and the
# scope guard flagged PCP's own write as agent over-reach. These paths are never
# an agent deliverable; they are excluded from gate inputs entirely.
_PCP_OPERATIONAL_PATHS = (
    ".pcp/token_ledger.yaml", ".pcp/telemetry.jsonl", ".pcp/decision_log.jsonl",
    ".pcp/brd.md", ".pcp/brd_items.yaml", ".pcp/coverage_audit.jsonl",
    ".pcp/escalations.yaml", ".pcp/prune_log.yaml", ".pcp/current_state.md",
    ".pcp/diff.md", ".pcp/notify_heartbeat.yaml",
    # Added 2026-07-27. `_write_progress` (added 07-24) writes this on every
    # single build attempt, so omitting it re-created the exact 07-17 bug the
    # comment above describes: PCP's own bookkeeping landing in changed_files,
    # polluting the judge diff and drawing scope-guard findings against the
    # agent. Any NEW file PCP writes under .pcp/ during a build attempt must be
    # added here at the same time it is introduced.
    ".pcp/build_progress.yaml",
    # run_log.py's pre/post audit bracket, added 2026-07-23 and never
    # registered here — found 2026-07-27 the same hour the rule above was
    # written, which is the point: the rule is not self-enforcing, so
    # test_no_unregistered_pcp_runtime_writer() now checks it mechanically.
    ".pcp/run_ledger.jsonl",
)
_PCP_OPERATIONAL_DIRS = (".pcp/evidence/", ".pcp/transcripts/")


# Paths `_auto_commit_criterion` must never stage. Two distinct hazards, one
# rule: any file that (a) is written continuously by PCP or the agent harness
# during a build and (b) is not an agent deliverable will, if committed by a
# worktree branch, break the merge that brings that branch home.
#
# 2026-07-25 was the `.claude/settings.json` case: every worktree wrote a
# DIFFERENT version of the same new path, so merging two branches was an
# add/add conflict. That got patched by naming those two files here.
#
# 2026-07-27 (signtool dogfood) was the same shape through the other door:
# `.pcp/token_ledger.yaml` and friends are TRACKED, and PCP appends to them in
# the main pcp_dir throughout the run by design. So at merge time the main
# repo has uncommitted changes to a tracked file the incoming branch also
# committed, and git refuses before it even starts:
#     "Your local changes to the following files would be overwritten by
#      merge: .pcp/token_ledger.yaml"
# Criterion A001 halted the build on exactly this, after A002 and A004 had
# already merged cleanly.
#
# Deriving this from _PCP_OPERATIONAL_PATHS rather than listing files again is
# the point: those tuples already define "PCP's own bookkeeping, not agent
# output", and every consumer of that idea should read the same source. Naming
# one offending file at a time is what let the identical bug return twice.
_AGENT_LOCAL_CONFIG = (
    ".claude/settings.json", ".claude/settings.local.json",
    # testmon's per-test dependency cache. Written on every build, differs
    # per worktree, and is not an agent deliverable -- precisely the shape
    # that broke wave merges twice on 2026-07-27 (.claude/settings.json as
    # an add/add conflict, .pcp/token_ledger.yaml as "your local changes
    # would be overwritten by merge"). Excluded before it can do it again.
    ".testmondata", ".testmondata-journal",
)

_AUTO_COMMIT_EXCLUDES = tuple(
    f":!{p}" for p in (*_AGENT_LOCAL_CONFIG, *_PCP_OPERATIONAL_PATHS)
) + tuple(f":!{d.rstrip('/')}" for d in _PCP_OPERATIONAL_DIRS)


def _is_pcp_operational(path: str) -> bool:
    norm = path.replace("\\", "/").removeprefix("./")
    return norm in _PCP_OPERATIONAL_PATHS or any(norm.startswith(d) for d in _PCP_OPERATIONAL_DIRS)


def _get_unstaged_files(cwd: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _get_staged_files(cwd: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _get_untracked_files(cwd: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _get_committed_files_since(cwd: Path, since_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", since_ref],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        return []
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def _get_changed_files_since(cwd: Path, since_ref: str | None) -> list[str]:
    """Everything the agent touched this criterion, however it left it:
    committed (diff since the criterion-start ref), staged, unstaged, or
    still untracked. Found dogfooding 2026-07-17 (round 2): the agent
    COMMITTED its work — perfectly reasonable, nothing told it not to — and
    the old staged+unstaged-only view reported 'No files were modified by
    the agent' against a 65-line committed implementation, then the gates
    judged an empty diff. An agent must not be able to make its work
    invisible to the gates by committing it."""
    files = set(_get_staged_files(cwd) + _get_unstaged_files(cwd) + _get_untracked_files(cwd))
    if since_ref:
        files.update(_get_committed_files_since(cwd, since_ref))
    return sorted(files)


def _get_working_diff(cwd: Path, since_ref: str | None = None) -> str:
    # :(exclude) pathspecs keep PCP's own operational writes (token ledger,
    # telemetry, evidence) out of the diff the LLM judges see — see
    # _PCP_OPERATIONAL_PATHS above for why. Diff base is the criterion-start
    # ref when given (covers work the agent committed), falling back to HEAD.
    excludes = [f":(exclude){p}" for p in _PCP_OPERATIONAL_PATHS] + \
               [f":(exclude){d.rstrip('/')}" for d in _PCP_OPERATIONAL_DIRS]
    base = since_ref or "HEAD"
    result = subprocess.run(
        ["git", "diff", base, "--", ".", *excludes],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        # Fallback to general diff
        result = subprocess.run(
            ["git", "diff", "--", ".", *excludes],
            capture_output=True, text=True, cwd=cwd,
        )
    out = result.stdout

    # `git diff` NEVER shows untracked files, so a criterion whose agent created
    # only NEW files and left them unstaged produced an empty diff here while
    # `_get_changed_files_since` (right above) correctly reported them. The
    # gates then judged nothing and returned "No diff provided; cannot assess
    # alignment" -- a guaranteed 0% BLOCK on work that plainly existed.
    #
    # Observed live 2026-07-27, signtool dogfood, pdf-document-storage/A004:
    # scope guard listed 3 modified files in the same attempt the alignment
    # gate reported no diff at all.
    #
    # This is the SAME bug the sibling function's docstring describes fixing on
    # 2026-07-17 ("an agent must not be able to make its work invisible to the
    # gates"). That fix taught the file LIST about all four states and left the
    # DIFF beside it knowing only three. Read-only on purpose: `git add -N`
    # would surface them too but mutates the index during what must stay a
    # pure gate evaluation.
    for path in _get_untracked_files(cwd):
        if _is_pcp_operational(path):
            continue
        shown = subprocess.run(
            ["git", "diff", "--no-index", "--", os.devnull, path],
            capture_output=True, text=True, cwd=cwd,
        )
        # --no-index exits 1 when the files differ, which is the normal case here.
        if shown.stdout:
            out += shown.stdout

    return out[:14000]


UI_KEYWORDS = (
    "render", "renders", "display", "displays", "dashboard", "portal",
    "screen", "view", "form", "ui", "page", "widget",
)


def _is_ui_facing_criterion(criterion: dict) -> bool:
    """Cheap, deterministic keyword check (rung 1 — no LLM call needed to
    decide whether to mention the design system). False negatives just mean
    a UI criterion doesn't get the design-system hint; false positives just
    mean a harmless, ignorable pointer gets included for a non-UI criterion.
    Neither costs anything beyond a few extra prompt tokens."""
    text = criterion.get("description", "").lower()
    return any(kw in text for kw in UI_KEYWORDS)


def _build_agent_prompt(
    pcp_dir: Path,
    module_name: str,
    criterion: dict,
    spec: dict,
) -> str:
    """First-attempt prompt. You have filesystem access — read .pcp/ context yourself
    instead of having it pasted here. Pasting it costs input tokens on every single
    criterion/attempt for content that's identical across the whole build run."""
    # Context routing (2026-07-18): the file list comes from the declarative
    # context_map (scenario -> files), not a hardcoded paste-adjacent list.
    # module_state routes to THIS module's generated state slice
    # (docs/built.md — a projection regenerated from acceptance.yaml), not
    # program-wide current_state.md: on a many-module project the global
    # file is mostly other modules' context — measured contamination, not
    # useful grounding. Falls back to current_state.md when the slice
    # doesn't exist yet (pre-docs-kit projects).
    from pcp import context_map
    always_files = context_map.resolve(pcp_dir, "always")
    state_files = context_map.resolve(pcp_dir, "module_state", module=module_name)
    read_list = [f"- {p}" for p in always_files + state_files] or [
        "- .pcp/objective.md", "- .pcp/architecture.md", "- .pcp/current_state.md",
    ]
    prompt_parts = [
        "You are an AI coding agent implementing an acceptance criterion for a program module.",
        "Your task is to write/modify code in the project to implement this feature.",
        f"Module: {module_name}",
        f"Criterion: [{criterion['id']}] {criterion['description']}",
        "",
        "Before editing, read these files yourself for context (don't ask — just Read them). "
        "Read ONLY these — they are routed for this specific criterion; pulling in other "
        ".pcp/ files adds noise, not grounding:",
        *read_list,
        "",
    ]

    # This criterion's own acceptance.yaml already declares the file it's
    # about (`target`, and `pattern` for ast_pattern checks) — found
    # 2026-07-08 that without this hint the agent spent several turns per
    # criterion re-discovering it via `find`/`grep`, real turns/cache_read
    # volume for information already on disk.
    target = criterion.get("target")
    if target:
        prompt_parts.append(
            f"This criterion's target file is `{target}` — start there instead of "
            f"searching the repo for it. If it doesn't exist yet, create it there."
        )
        pattern = criterion.get("pattern")
        if pattern:
            prompt_parts.append(f"It must satisfy this pattern: `{pattern}`")
        prompt_parts.append("")

    # Learned-decision injection (ECC "instincts" reference-pattern, 2026-07-17):
    # decision_log.jsonl was captured but never fed back — every criterion
    # agent re-discovered root causes / library picks / workarounds earlier
    # sessions already distilled. Deterministic selection, bounded count+chars,
    # zero LLM cost (see decision_log.select_relevant).
    decision_lines = decision_log.format_for_prompt(pcp_dir, module_name)
    if decision_lines:
        prompt_parts.append(
            "## Prior technical decisions in this project (distilled from earlier "
            "sessions — treat as established context, don't re-derive or contradict "
            "them without saying why):"
        )
        prompt_parts += decision_lines
        prompt_parts.append("")

    # Librarian retrieval (2026-07-20, swarm-role design): deterministic
    # keyword-overlap scan over EXISTING definitions in the project, so this
    # criterion's builder doesn't independently re-explore the codebase for
    # a pattern another module already has. Rung-4-shaped (retrieval, not a
    # conversational search agent) — pure query/response, never corrects or
    # blocks. Bounded count/chars, zero LLM cost, same Token Discipline
    # posture as the decision-log injection above.
    if os.environ.get("PCP_BUILD_INJECT_LIBRARIAN", "1") != "0":
        librarian_lines = librarian.format_for_prompt(pcp_dir.parent, criterion)
        if librarian_lines:
            prompt_parts.append(
                "## Possibly-related existing code in this project (keyword match on "
                "this criterion's own description — not verified relevance, check before "
                "reusing):"
            )
            prompt_parts += librarian_lines
            prompt_parts.append("")

    # Rung-specific implementation guidance (2026-07-18): the tier is already
    # declared — point the agent at the guide's process + search-first list
    # for exactly that rung. One line; the guide is read on demand, never
    # pasted (Token Discipline).
    declared_tier = criterion.get("logic_tier")
    if isinstance(declared_tier, int):
        prompt_parts.append(
            f"This criterion declares logic_tier={declared_tier}. Before implementing, read "
            f"the 'Rung {declared_tier}' section of `.pcp/logic_tier_guide.md` (if present) — "
            "it gives the implementation process and what to SEARCH FOR before building "
            "(existing packages/models/patterns). If your implementation ends up needing a "
            "different rung than declared, STOP and say so in your summary rather than "
            "quietly building at the wrong tier — the wave gate checks tier honesty."
        )
        prompt_parts.append("")

    if _is_ui_facing_criterion(criterion):
        reference_image = criterion.get("reference_image")
        reference_line = (
            f" A reference image is declared for this criterion at `{reference_image}` — "
            "look at it before building; it's also fed to the automated visual-quality "
            "check as a comparison target after you finish (layout/structure similarity, "
            "not pixel-perfect)."
            if reference_image else ""
        )
        recipes_path = pcp_dir / "ui_kit_recipes.yaml"
        ui_kit_line = (
            " If `.pcp/ui_kit_recipes.yaml` exists, read it: it maps this screen's "
            "archetype(s) to the organisms (data-table, primary-nav, modal, ...) it needs, "
            "and each organism to a real shadcn/ui component to vendor (use the shadcn MCP "
            "server if available, or `npx shadcn add <component>` directly) rather than "
            "hand-rolling markup — PCP doesn't maintain UI component code itself, shadcn "
            "already does. Declare `screen_archetypes` and `ui_organisms` on this criterion "
            "in acceptance.yaml matching what you actually built; the wave-merge gate "
            "checks these against the recipe and against real imports in your target file."
            if recipes_path.exists() else ""
        )
        prompt_parts.append(
            "This criterion renders user-facing UI. Read `.pcp/design_system.md` first "
            "and apply its established tokens/conventions rather than deciding a look "
            "fresh — if it's still the empty scaffold, this is the first UI screen: "
            "establish the system now (see the `pcp-ui-design` skill) and write it there "
            "so later screens stay consistent instead of each looking like a different "
            f"vanilla template.{reference_line}{ui_kit_line} Before finishing, add a "
            "`design_justification` block to this criterion in acceptance.yaml: "
            "`checklist_passed` (which design-system conventions this screen actually "
            "followed), `jtbd_framing` (one sentence, 'when a user is X, this lets them "
            "Y' — not a restatement of the description), and `deviations_from_system` if "
            "this screen needed a new pattern the system didn't have yet. If a "
            "`webapp-testing` skill is available, use it to actually load the running "
            "page and verify it renders/behaves as intended before finishing — don't "
            "just trust that the code compiles."
        )
        prompt_parts.append("")

    prompt_parts += [
        "## Module Specification",
        yaml.dump(spec, default_flow_style=False),
        "",
        "Follow TDD: write a failing test for this criterion first, confirm it fails, "
        "then write the implementation and confirm the test passes. The full test suite, "
        "lint, and a SAST/secret scan will run against your changes after you finish — "
        "fix anything those would flag before considering the criterion done.",
        "Use editing tools to modify files and run tests to verify your implementation.",
        "Git rules: stay on the current branch — never create or switch branches. "
        "You may commit your work or leave it uncommitted; the build loop measures "
        "everything you changed since this criterion started either way. Never "
        "`git add` build artifacts (__pycache__, *.pyc, node_modules, dist, coverage "
        "files) — committed artifacts break the module merge step.",
    ]
    return "\n".join(prompt_parts)


def _build_escalation_prompt(pcp_dir: Path, module_name: str, criterion: dict, spec: dict,
                             attempt_history: list[str]) -> str:
    """Final-attempt (escalated-model) prompt: a FRESH session — never a
    --resume of the failed attempts. Contaminated retry context raises error
    rates ~7x (CCRM, arXiv:2605.08563); the escalated model gets a structured
    summary of the prior failures instead of their raw trajectory
    (summarize-don't-replay, arXiv:2604.16529). Costs a fresh repo
    exploration — deliberately: on the final attempt before human escalation,
    a clean read of the problem is worth more than the cached context."""
    base = _build_agent_prompt(pcp_dir, module_name, criterion, spec)
    history = "\n".join(f"- {h}" for h in attempt_history) or "- (no structured history captured)"
    return base + "\n".join([
        "",
        "## Prior attempts on this criterion FAILED — summary (you are a fresh session; "
        "do not repeat these approaches without addressing why they failed):",
        history,
        "",
        "Diagnose from the current state of the working tree (their partial work may still "
        "be present) and take a genuinely different approach where the summary suggests the "
        "previous one was structurally wrong.",
    ])


def _build_retry_prompt(constraint_feedback: str) -> str:
    """Follow-up prompt for a --resume'd session. No re-pasted context — the agent
    already has it from the same session's earlier turn."""
    return "\n".join([
        "⚠️ Your previous attempt at this criterion was BLOCKED by quality/architecture gates:",
        "",
        constraint_feedback,
        "",
        "Fix these violations in your next edits. Make sure to adhere to all principles. "
        "This is the same session as your last attempt — don't re-read files you've already "
        "reviewed unless something changed.",
    ])


# Sentinel distinct from None: None means "tool not detected", NOTSET means
# "not tool-based, always applicable" (layer1, architect-review, gate).
_NOTSET = object()


def _qa_record(
    pcp_dir: Path, ctx: dict, check: str, errors: list[str], meta: dict | None = None,
    *, control_id: str | None = None, files: list[str] | None = None,
    tool: str | None = _NOTSET, result: str | None = None, evidence_path: str | None = None,
) -> None:
    """Records one gate outcome. `result` resolution order: explicit override,
    then "skipped" if a tool-based check found no tool installed, then
    block/pass from `errors`. A skip must never collapse into "pass" — that's
    what makes an unenforced control invisible in the audit trail.

    evidence_path: relative path (under pcp_dir) to the FULL, untruncated raw
    artifact for this check (test output, lint issue list, judge response) —
    see evidence.py. telemetry only ever stored a truncated error summary;
    this is the pointer to actual proof, written on every outcome including
    a pass, not just when something blocks."""
    if result is None:
        if tool is not _NOTSET and tool is None:
            result = "skipped"
        else:
            result = "block" if errors else "pass"
    elif result == "pass" and errors:
        # Same invariant as _wave_record (see there for the full reasoning):
        # an advisory check forces result="pass" to mean "found things, don't
        # block", and recording that literally made the audit trail claim a
        # clean pass. CTRL-032 (architect pre-flight) did exactly this, and
        # the test suite asserted the falsified value rather than catching it.
        result = "advisory"

    # Evidence-integrity self-check, 2026-07-21: a block with no real
    # evidence behind it is itself an anomaly, not a normal outcome to
    # trust silently -- this is the exact shape of the SAST-phantom-block
    # incident (qa.py's semgrep wrapper conflated a tool failure with a
    # real finding; the "block" had nothing behind it, only caught because
    # a human happened to read an empty evidence file). This tripwire is
    # deliberately generic -- it doesn't know or care which check produced
    # the block, so it still fires the next time this bug SHAPE recurs in
    # a different tool, not just the one instance patched tonight. Multi-
    # hour unattended runs have nobody reading evidence files live, so this
    # has to be loud (console) and durable (escalations.yaml), not just
    # printed and forgotten.
    if result == "block" and evidence_path:
        try:
            evidence_empty = not (pcp_dir / evidence_path).read_text().strip()
        except Exception:
            evidence_empty = False  # can't read it -- don't compound one failure into a false alarm
        if evidence_empty:
            console.print(
                f"[red bold]Evidence-integrity anomaly:[/red bold] check '{check}' blocked "
                f"({len(errors)} finding(s)) but its evidence file ({evidence_path}) is empty -- "
                "this block is likely not grounded in a real finding (tool-failure-misreported-"
                "as-finding, the 2026-07-21 SAST incident shape). Treat it with suspicion."
            )
            from pcp import escalations
            with _STATE_LOCK:
                escalations.record(
                    pcp_dir, ctx["module"], ctx["criterion_id"], route="evidence-integrity-anomaly",
                    findings=[
                        f"Check '{check}' reported a block with an empty evidence file -- the block "
                        "is likely ungrounded (a tool failure misreported as a real finding), not a "
                        "genuine issue with the criterion's code. Verify before trusting this block.",
                    ],
                )

    usage = (meta or {}).get("usage", {})
    with _STATE_LOCK:
        telemetry.record(
            pcp_dir,
            cycle="qa", cycle_number=ctx["attempt"], check=check, control_id=control_id,
            module=ctx["module"], submodule=ctx.get("submodule"), criterion_id=ctx["criterion_id"],
            files=files or ctx.get("files") or [],
            result=result, errors=errors, error_count=len(errors), evidence_path=evidence_path,
            model=(meta or {}).get("model"), session_id=(meta or {}).get("session_id"),
            token_input=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
            token_output=usage.get("output_tokens", 0),
            token_cache_read=usage.get("cache_read_input_tokens", 0),
            cost_usd=(meta or {}).get("cost_usd"), duration_ms=(meta or {}).get("duration_ms"),
        )


def _apply_rule_recovery(pcp_dir: Path, ctx: dict, rule: dict, violation_msg: str) -> None:
    """ABC contract-shape reference pattern (arXiv:2602.22302, see
    docs/research-rigidity-vs-reliability-2026-07.md) -- Governance is
    already `severity` (hard_block/advisory), not duplicated here.
    `recovery` is the one contract field with real behavior in this
    version: 'escalate' immediately logs an escalation entry the moment
    this rule fires, instead of only escalating after a criterion
    exhausts all 3 build attempts. 'retry'/'quarantine'/'block' are
    declared in the schema for completeness but don't change behavior yet
    -- same honest-scope posture as every other partially-built check in
    this catalog (e.g. CTRL-016's build_fresh carve-out)."""
    if rule.get("contract", {}).get("recovery") != "escalate":
        return
    from pcp import escalations
    escalations.record(
        pcp_dir, ctx["module"], ctx["criterion_id"], route="human",
        findings=[f"Immediate escalation (contract.recovery=escalate) on rule [{rule.get('id')}]: {violation_msg}"],
    )


def _run_layer1_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> list[str]:
    """Run AST check logic and return violations. Deterministic — no LLM/tokens."""
    ci_rules_path = pcp_dir / "ci_rules.yaml"
    violations: list[str] = []

    if not ci_rules_path.exists():
        _qa_record(pcp_dir, ctx, "layer1", violations, control_id="CTRL-004", files=changed_files, tool=None)
        return violations

    try:
        data = load_yaml(ci_rules_path)
        ast_rules = [r for r in data.get("rules", []) if r.get("check") == "ast_pattern"]
        file_rules = [r for r in data.get("rules", []) if r.get("check") == "file_exists"]
        protected_rules = [r for r in data.get("rules", []) if r.get("check") == "protected_path"]
        from pcp.commands.check import _run_ast_rule, run_file_exists_rule, run_protected_path_rule, get_module_names
        for r in ast_rules:
            if r.get("severity") == "hard_block":
                v = _run_ast_rule(r, changed_files, project_root)
                if v:
                    msg = f"AST Rule [{r['id']}] {r['name']} violation: {', '.join(v)}"
                    if r.get("message"):
                        msg += f" → Fix: {r['message']}"
                    violations.append(msg)
                    _apply_rule_recovery(pcp_dir, ctx, r, msg)
        for r in protected_rules:
            if r.get("severity") == "hard_block":
                v = run_protected_path_rule(r, changed_files)
                if v:
                    msg = f"Protected Path Rule [{r['id']}] {r['name']} violation: {', '.join(v)}"
                    if r.get("message"):
                        msg += f" → Fix: {r['message']}"
                    violations.append(msg)
                    _apply_rule_recovery(pcp_dir, ctx, r, msg)
        if file_rules:
            module_names = get_module_names(pcp_dir)
            for r in file_rules:
                if r.get("severity") == "hard_block":
                    v = run_file_exists_rule(r, project_root, module_names)
                    if v:
                        msg = f"File Rule [{r['id']}] {r['name']} violation: {', '.join(v)}"
                        if r.get("message"):
                            msg += f" → Fix: {r['message']}"
                        violations.append(msg)
                        _apply_rule_recovery(pcp_dir, ctx, r, msg)
    except Exception:
        violations.append("Invalid ci_rules.yaml schema")

    _qa_record(pcp_dir, ctx, "layer1", violations, control_id="CTRL-004", files=changed_files, tool="ci_rules.yaml")
    return violations


def _run_test_suite_check(pcp_dir: Path, project_root: Path, ctx: dict) -> list[str]:
    """Scoped to the blast radius of this attempt's changed files (impact.py):
    the changed module(s), every module that transitively depends on them, and
    the modularity drop-tests. The unscoped full suite is the wave-merge gate's
    job (_run_wave_merge_gate's own qa.run_test_suite call) -- it is not run
    here on every one of up to 3 attempts per criterion.

    This was the documented design from the start but sat behind an opt-in flag
    that defaulted off, so the full suite ran every time regardless. Measured
    2026-07-27 on ontology-foundry: 1,098 tests / ~7m46s per attempt, versus 478
    scoped. PCP_QA_FULL_SUITE=1 restores the old behaviour."""
    result = qa.run_test_suite(project_root, pcp_dir=pcp_dir, changed_files=ctx.get("files"))
    if result.get("scoped_to"):
        detail = f"[dim]Test suite scoped to impacted modules: {', '.join(result['scoped_to'])}"
        if result.get("incremental"):
            detail += " (testmon: only tests whose dependencies changed)"
        # Name the binary. A gate run by the wrong pytest -- the global one,
        # because the project venv was not on PATH -- passes exactly like a
        # correct one. Saying which interpreter produced the result is the
        # cheapest defence against that whole class.
        if result.get("pytest_bin"):
            detail += f" via {result['pytest_bin']}"
        console.print(detail + "[/dim]")
    violations: list[str] = []
    evidence_path = None
    if result["tool"]:
        evidence_path = evidence.store(
            pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "test-suite", result["output"],
        )
    if result["tool"] and not result["passed"]:
        violations.append(
            f"Test suite ({result['tool']}) FAILED — full output: {evidence_path}\n{result['output'][-1500:]}"
        )
    _qa_record(pcp_dir, ctx, "test-suite", violations, control_id="CTRL-001", tool=result["tool"], evidence_path=evidence_path)
    return violations


def _report_gate_skip_anomaly(budget: "_BuildBudget", check: str, tool: str, skipped: bool) -> None:
    """Shared by lint/SAST -- see _BuildBudget.record_gate_skip_signal.
    Only called when the tool was actually detected (never for a genuinely
    absent tool, which is expected stable config, not an anomaly)."""
    if not tool:
        return
    if budget.record_gate_skip_signal(check, skipped):
        streak = budget.gate_skip_streaks[check]
        console.print(
            f"[red bold]Gate anomaly suspected:[/red bold] '{check}' ({tool}) has silently skipped "
            f"{streak} consecutive attempts instead of actually checking anything. This usually means "
            "the tool itself is broken or misconfigured, not that the code is clean -- verify it before "
            "trusting any further pass/skip result from this gate this run."
        )


def _run_lint_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict, budget: "_BuildBudget") -> list[str]:
    """Lint on changed files only. Skips (never blocks) if no linter detected."""
    result = qa.run_lint(project_root, changed_files)
    if result.get("skipped"):
        console.print(f"[yellow]Lint tool issue (not a finding, not blocking):[/yellow] {result['skipped']}")
    _report_gate_skip_anomaly(budget, "lint", result["tool"], bool(result.get("skipped")))
    violations: list[str] = []
    evidence_path = None
    if result["tool"]:
        evidence_path = evidence.store(
            pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "lint", "\n".join(result["issues"]),
        )
    if result["tool"] and not result["passed"]:
        issues = "\n".join(result["issues"][:10])
        violations.append(f"Lint ({result['tool']}) found issues — full list: {evidence_path}\n{issues}")
    _qa_record(pcp_dir, ctx, "lint", violations, control_id="CTRL-002", files=changed_files, tool=result["tool"], evidence_path=evidence_path)
    return violations


def _run_sast_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict, budget: "_BuildBudget") -> list[str]:
    """SAST + secret-scan via semgrep, if installed. Scoped to changed files."""
    result = qa.run_sast(project_root, changed_files)
    if result.get("skipped"):
        console.print(f"[yellow]SAST tool issue (not a finding, not blocking):[/yellow] {result['skipped']}")
    _report_gate_skip_anomaly(budget, "sast", result["tool"], bool(result.get("skipped")))
    violations: list[str] = []
    evidence_path = None
    if result["tool"]:
        evidence_path = evidence.store(
            pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "sast", "\n".join(result["findings"]),
        )
    if result["tool"] and not result["passed"]:
        findings = "\n".join(result["findings"][:10])
        violations.append(f"SAST ({result['tool']}) found issues — full list: {evidence_path}\n{findings}")
    _qa_record(pcp_dir, ctx, "sast", violations, control_id="CTRL-003", files=changed_files, tool=result["tool"], evidence_path=evidence_path)
    return violations


VERIFY_SYSTEM_PROMPT = (
    "You are an adversarial verifier for AI-generated code-review findings. "
    "You are given a diff and a numbered list of findings another reviewer flagged as "
    "blocking issues. For each finding, decide whether it is actually grounded in the "
    "diff shown -- concrete, specific, and checkable against the code present, not vague "
    "or referring to code/behavior that isn't actually there. Do not look for NEW issues "
    "of your own -- only judge whether each GIVEN finding holds up. Default to "
    "refuted=true whenever you cannot confirm a finding directly against the diff shown."
)


def _verify_block_findings(
    pcp_dir: Path, diff: str, findings: list[str], ctx: dict, check: str, control_id: str,
) -> tuple[list[str], list[str]]:
    """Adversarial second pass over a gate/architect-review call's own BLOCK
    findings before they're trusted enough to fail a criterion -- reference-
    pattern borrowed from CodeRabbit's judge-model verification layer
    (scores each finding against gathered context, drops what it can't
    ground, before it ever reaches a human). Batched as ONE extra call per
    check, not one per finding, to stay inside Token Discipline.

    Fails OPEN on any verifier error (timeout, bad JSON, call failure):
    keeps every original finding unchanged rather than risk silently
    swallowing a real block because the verifier itself broke. The
    asymmetry is deliberate -- a hallucinated BLOCK that slips through
    costs one wasted retry attempt; a real BLOCK silently dropped ships an
    actual defect, a strictly worse outcome.

    Returns (kept, dropped_with_reason).
    """
    if not findings:
        return [], []

    # Deterministic pre-check (CodeRabbit pattern, validated by the grounded-
    # code-review production system arXiv:2510.10290): a finding that cites a
    # file path appearing nowhere in the diff is dropped at zero LLM cost
    # before the verifier call. Only fires on findings that DO cite a path —
    # conceptual findings with no file reference pass straight through to the
    # LLM verifier, which judges substance.
    # A file counts as "in scope" if its basename appears in the diff text OR
    # in the criterion's changed-files list — the diff is capped at 14k chars,
    # so text absence alone is not proof of fabrication.
    known_basenames = {Path(p).name for p in (ctx.get("files") or [])}
    pre_kept, pre_dropped = [], []
    for f in findings:
        cited = re.findall(r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|c|cpp|h|html|css|yaml|yml|json|toml|md)\b", f)
        # Paths PCP itself wrote into the finding text (evidence pointers) don't count as citations.
        cited = [p for p in cited if not p.replace("\\", "/").lstrip("./").startswith((".pcp/", "evidence/"))]
        if cited and not any(p.split("/")[-1] in diff or p.split("/")[-1] in known_basenames for p in cited):
            pre_dropped.append(f"{f}  [dropped by deterministic pre-check: cites {cited[0]} which appears nowhere in the diff or changed files]")
        else:
            pre_kept.append(f)
    findings = pre_kept
    if not findings:
        if pre_dropped:
            console.print(f"[dim]{check} pre-check dropped {len(pre_dropped)} finding(s) citing files absent from the diff.[/dim]")
        return [], pre_dropped

    numbered = "\n".join(f"[{i}] {f}" for i, f in enumerate(findings))
    prompt = (
        f"## Diff\n{diff[:14000]}\n\n"
        f"## Findings to verify\n{numbered}\n\n"
        '## Respond with JSON only\n'
        '{"verdicts": [{"index": 0, "refuted": false, "reason": "..."}, ...]} '
        "-- exactly one entry per finding above, in order."
    )
    # Judge decorrelation (2026-07-17, from the academic sweep): the original
    # finding comes from JUDGE_MODEL (Haiku); a same-model verifier adds
    # almost no independent signal (correlated-judges result, arXiv:2605.29800
    # "nine judges ≈ two effective votes"; same-family preference leakage,
    # arXiv:2502.01534). Default verifier is therefore a DIFFERENT model
    # (BUILD_MODEL/Sonnet — acceptable cost since this only runs on BLOCK
    # findings, which are rare). PCP_VERIFIER_MODEL overrides for teams that
    # can route cross-vendor — honestly noted: Sonnet-verifying-Haiku is
    # cross-model but still same-vendor, weaker decorrelation than the
    # literature's ideal; the deterministic pre-check above is the fully
    # decorrelated layer.
    verifier_model = os.environ.get("PCP_VERIFIER_MODEL") or llm.BUILD_MODEL
    try:
        res, meta = llm.call_json(
            VERIFY_SYSTEM_PROMPT, prompt, model=verifier_model, pcp_dir=pcp_dir,
            command=f"build-{check}-verify", return_meta=True,
        )
    except Exception as e:
        console.print(f"[yellow]Warning: {check} verification call failed, keeping all findings unverified: {e}[/yellow]")
        _qa_record(pcp_dir, ctx, f"{check}-verify", [f"call failed: {e}"], control_id=control_id, result="error")
        return findings, pre_dropped

    verdicts = {v.get("index"): v for v in res.get("verdicts", []) if isinstance(v, dict)}

    # Opt-in two-verifier ensemble (FUSE, arXiv:2604.18547; disagreement-as-
    # signal rather than majority-silencing). Second verifier gets an
    # INVERTED framing (confirm, don't refute) — prompt-level decorrelation.
    # A finding is dropped only when BOTH agree it's ungrounded; disagreement
    # keeps the finding, tagged, so the retry agent (and telemetry) see that
    # verification was contested. PCP_VERIFIER_ENSEMBLE=1 to enable — one
    # extra call per check, only on BLOCK findings.
    verdicts2: dict = {}
    if os.environ.get("PCP_VERIFIER_ENSEMBLE") == "1":
        confirm_system = (
            "You are a supportive verifier for code-review findings: for each GIVEN finding, "
            "try to CONFIRM it against the diff. Mark refuted=true only if you find clear "
            "evidence the finding is wrong or refers to code not present."
        )
        try:
            res2, _ = llm.call_json(
                confirm_system, prompt, model=verifier_model, pcp_dir=pcp_dir,
                command=f"build-{check}-verify2", return_meta=True,
            )
            verdicts2 = {v.get("index"): v for v in res2.get("verdicts", []) if isinstance(v, dict)}
        except Exception:
            verdicts2 = {}

    kept: list[str] = []
    dropped: list[str] = list(pre_dropped)
    for i, f in enumerate(findings):
        v = verdicts.get(i)
        refuted1 = bool(v and v.get("refuted"))
        if not refuted1:
            kept.append(f)
            continue
        v2 = verdicts2.get(i)
        if v2 is not None and not v2.get("refuted"):
            kept.append(f"{f}  [verifier disagreement — kept, contested]")
        else:
            dropped.append(f"{f}  [dropped by verifier: {(v or {}).get('reason', '(no reason given)')}]")

    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], f"{check}-verify",
        json.dumps(res, indent=2),
    )
    _qa_record(
        pcp_dir, ctx, f"{check}-verify", [], meta, control_id=control_id,
        evidence_path=evidence_path, result="pass",
    )
    if dropped:
        console.print(f"[dim]{check} verifier dropped {len(dropped)} ungrounded finding(s) before they could block:[/dim]")
        for d in dropped:
            console.print(f"  [dim]· {d}[/dim]")
    return kept, dropped


def _dismissal_context(pcp_dir: Path, module: str) -> str:
    """Learnings from past human overrides (Greptile feedback-loop reference
    pattern: storing dismissals and reusing them took comments-addressed from
    19%→55%+). PCP's dismissal signal = attributed [pcp-bypass] entries for
    this module — a human explicitly overrode a gate there. Surfaced as
    context to the next judge call so equivalent findings aren't re-raised
    without new evidence. Deterministic read, no LLM."""
    try:
        import yaml as _yaml
        path = pcp_dir / "bypass_log.yaml"
        if not path.exists():
            return ""
        data = _yaml.safe_load(path.read_text()) or {}
        entries = [e for e in data.get("bypasses", [])
                   if module in (e.get("modules") or [])][-5:]
        if not entries:
            return ""
        lines = [f"- {e.get('timestamp', '')}: {e.get('reason', '')}" for e in entries]
        return (
            "\n\nPRIOR HUMAN OVERRIDES in this module (gate findings a human explicitly "
            "bypassed — do not re-raise equivalent findings without new evidence):\n"
            + "\n".join(lines) + "\n"
        )
    except Exception:
        return ""


def _criterion_scope_framing(ctx: dict) -> str:
    """Prepended to build-loop judge prompts (gate + architect-review) so a
    single criterion's diff is judged as an increment, not the finished
    product. Found dogfooding 2026-07-17: without this, the alignment gate
    scored criterion 1 of 13 against the ENTIRE target state and blocked all
    3 attempts with 'regressions' like 'no CLI entry point' — functionality
    that simply belonged to later criteria. Incompleteness is not drift.
    The standalone `pcp gate` command (a real whole-PR review) deliberately
    keeps its original framing — this applies only inside the build loop."""
    return (
        "IMPORTANT CONTEXT: this diff implements exactly ONE acceptance criterion of an "
        f"in-progress multi-criterion build — [{ctx['criterion_id']}] "
        f"{ctx.get('criterion_description', '')} (module '{ctx['module']}'). "
        "Most other criteria and modules are intentionally NOT built yet. Judge only "
        "whether THIS increment moves correctly: flag genuine contradictions of the "
        "objective/target state, rule violations, or code that moves away from them. "
        "Functionality that is merely missing because it belongs to another criterion or "
        "module is NOT a regression — do not list it and do not lower the score for it.\n\n"
    )


def _gate_infrastructure_failure(check: str, exc: Exception) -> list[str]:
    """A gate that COULD NOT RUN is not a gate that PASSED.

    Both LLM gates used to `return []` when `llm.call_json` raised -- a rate
    limit, a timeout, an unauthenticated CLI. An empty finding list means "no
    problems found", so a criterion whose review never actually happened was
    marked complete, committed and merged. In an unattended run nobody reads
    the console warning that was the only signal.

    This is the exact bug class already fixed twice in `qa.py` (the semgrep
    phantom block, the QA timeout masking): conflating "the tool could not
    run" with "the tool found nothing". Those fixes were made file-locally
    instead of as a rule, which is why the same shape survived here.

    Returning a blocking finding is the honest answer, and it composes
    correctly with the retry loop: a transient failure clears on attempt 2 or
    3, a persistent one exhausts the attempts and escalates -- which is right,
    because PCP genuinely could not verify the work and must not claim it did.

    PCP_ALLOW_UNVERIFIED_GATES=1 restores the old advisory behavior for anyone
    deliberately running without LLM budget. Opt-in and loud, never default --
    it means completed criteria carry no LLM review at all."""
    if os.environ.get("PCP_ALLOW_UNVERIFIED_GATES") == "1":
        console.print(
            f"[yellow]{check}: gate could not run, and PCP_ALLOW_UNVERIFIED_GATES=1 "
            f"is set — treating as advisory. This criterion carries NO {check} review.[/yellow]"
        )
        return []
    # Deliberately does NOT lead with the escape hatch. Reported from
    # ontology-foundry 2026-07-27: a transient malformed-JSON response cost
    # three attempts on one criterion, and the remedy this message offered was
    # "turn the gate off" -- for the same review that had caught a real
    # path-traversal vulnerability an hour earlier. Offering "skip the check"
    # as the cure for a flaky check points at the wrong lever, so the mechanical
    # causes come first and the opt-out is named last, with what it costs.
    return [
        f"{check}: gate could not be evaluated ({exc}). This is an infrastructure "
        f"failure, not a code finding — the review never ran, so the criterion "
        f"cannot be marked verified.\n"
        f"  Most likely: a transient LLM/CLI failure — re-run, malformed JSON is "
        f"already retried {os.environ.get('PCP_LLM_JSON_RETRIES', '2')}x internally.\n"
        f"  Then check: is `claude` authenticated, is a rate limit active, is "
        f"PCP_LLM_TIMEOUT (default 300s) too low for this diff?\n"
        f"  Last resort: PCP_ALLOW_UNVERIFIED_GATES=1 completes criteria with NO "
        f"{check} review at all — this gate catches real defects, so disabling it "
        f"to get past a flaky run trades a correctness check for a convenience."
    ]


def _run_architect_review(pcp_dir: Path, diff: str, changed_files: list[str], ctx: dict) -> list[str]:
    """Run architect review and return BLOCK findings that survive adversarial verification."""
    from pcp.commands.architect_review import SYSTEM_PROMPT, _build_prompt, _load_persona, _load_kb
    persona = _load_persona(pcp_dir)
    architecture = (pcp_dir / "architecture.md").read_text() if (pcp_dir / "architecture.md").exists() else ""
    kb = _load_kb(pcp_dir, changed_files)

    prompt = _criterion_scope_framing(ctx) + _build_prompt(persona, architecture, kb, diff, "diff") + _dismissal_context(pcp_dir, ctx["module"])
    try:
        res, meta = llm.call_json(
            SYSTEM_PROMPT, prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir,
            command="build-architect-review", return_meta=True,
        )
    except Exception as e:
        console.print(f"[red]Architect review call failed: {e}[/red]")
        _qa_record(
            pcp_dir, ctx, "architect-review", [f"call failed: {e}"],
            control_id="CTRL-005", files=changed_files, result="error",
        )
        return _gate_infrastructure_failure("architect-review", e)

    blocks = []
    for f in res.get("findings", []):
        if f.get("severity") == "BLOCK":
            blocks.append(f"{f.get('location', 'general')}: {f.get('finding', '')} (Principle: {f.get('principle', '')}) → Fix: {f.get('fix', '')}")
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "architect-review", json.dumps(res, indent=2),
    )
    kept, _dropped = _verify_block_findings(pcp_dir, diff, blocks, {**ctx, "files": changed_files}, "architect-review", "CTRL-005")
    _qa_record(pcp_dir, ctx, "architect-review", kept, meta, control_id="CTRL-005", files=changed_files, evidence_path=evidence_path)
    return kept


def _run_gate_check(pcp_dir: Path, diff: str, ctx: dict) -> list[str]:
    """Run gate review and return block issues that survive adversarial verification."""
    from pcp.commands.gate import SYSTEM_PROMPT, _build_prompt, _load_llm_rules
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    target_state = (pcp_dir / "target_state.md").read_text() if (pcp_dir / "target_state.md").exists() else ""
    current_state = (pcp_dir / "current_state.md").read_text() if (pcp_dir / "current_state.md").exists() else ""
    llm_rules = _load_llm_rules(pcp_dir)

    prompt = _criterion_scope_framing(ctx) + _build_prompt(objective, target_state, current_state, diff, llm_rules) + _dismissal_context(pcp_dir, ctx["module"])
    try:
        res, meta = llm.call_json(
            SYSTEM_PROMPT, prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir,
            command="build-gate-check", return_meta=True,
        )
    except Exception as e:
        console.print(f"[red]Gate check call failed: {e}[/red]")
        _qa_record(pcp_dir, ctx, "gate", [f"call failed: {e}"], control_id="CTRL-006", result="error")
        return _gate_infrastructure_failure("gate", e)

    rec = res.get("recommendation", "merge")
    score = res.get("alignment_score", 1.0)
    issues = []
    if rec == "block" or score < 0.4:
        issues.append(f"PR alignment recommendation is BLOCK (Score: {score:.0%}). Summary: {res.get('summary', '')}")
        for r in res.get("regressions", []):
            issues.append(f"Regression: {r}")
        for v in res.get("llm_rule_violations", []):
            issues.append(f"Violation: {v}")
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "gate", json.dumps(res, indent=2),
    )
    kept, _dropped = _verify_block_findings(pcp_dir, diff, issues, ctx, "gate", "CTRL-006")
    _qa_record(pcp_dir, ctx, "gate", kept, meta, control_id="CTRL-006", evidence_path=evidence_path)
    return kept


def _prior_ui_screens_checked(pcp_dir: Path, ctx: dict) -> int:
    """Count distinct (module, criterion_id) pairs that already went through
    the design-consistency check, excluding this criterion's own -- the
    "how many screens has this project already built" signal progressive
    tightening needs. Deterministic, reads telemetry.jsonl only, no LLM."""
    seen = set()
    for rec in telemetry.load(pcp_dir):
        if rec.get("check") != "design-consistency":
            continue
        key = (rec.get("module"), rec.get("criterion_id"))
        if key == (ctx["module"], ctx["criterion_id"]):
            continue
        seen.add(key)
    return len(seen)


def _design_establishing_window() -> int:
    """First N UI-facing criteria are establishing the design system --
    findings there are exploration, not drift. Configurable since what
    counts as "established" genuinely varies by project size."""
    return int(os.environ.get("PCP_DESIGN_ESTABLISHING_SCREENS", "2"))


def _run_design_consistency_check(pcp_dir: Path, project_root: Path, criterion: dict, ctx: dict) -> None:
    """PCP Design lifecycle, stage 4 (Verify). Advisory only — never returned
    into block_findings, never blocks a criterion. Only fires for UI-facing
    criteria once .pcp/design_system.md has real established color tokens
    (not the empty scaffold): flags hardcoded hex color literals in the
    criterion's target file as a heuristic signal the screen may not be
    using the project's own design system. Not proof either way — a
    legitimate reason to hardcode a specific value (a brand-mandated exact
    color) is common; this surfaces a signal for human review, same posture
    as pcp audit's dead-code findings.

    Progressive tightening (2026-07-20, research backlog item 5, "first
    screen establishes, later screens conform harder"): once a project has
    already built PCP_DESIGN_ESTABLISHING_SCREENS UI screens against an
    established system, the SAME findings read as drift from a known
    pattern, not exploration -- reworded accordingly. Deliberately stays
    advisory-only regardless of screen count (never joins block_findings) --
    escalating an unmeasured advisory check straight to a hard gate is
    exactly the shortcut this codebase's own warn-first rollout doctrine
    exists to avoid; false-positive rate isn't measured yet at either tier."""
    if not _is_ui_facing_criterion(criterion):
        return

    design_system_path = pcp_dir / "design_system.md"
    if not design_system_path.exists() or "(not yet established)" in design_system_path.read_text():
        _qa_record(pcp_dir, ctx, "design-consistency", [], control_id="CTRL-013", tool=None)
        return

    target = criterion.get("target")
    target_path = project_root / target if target else None
    if not target_path or not target_path.is_file():
        _qa_record(pcp_dir, ctx, "design-consistency", [], control_id="CTRL-013", tool=None)
        return

    content = target_path.read_text(errors="replace")
    prior_screens = _prior_ui_screens_checked(pcp_dir, ctx)
    established = prior_screens >= _design_establishing_window()
    severity_prefix = "established-system drift" if established else "exploration"

    hex_matches = re.findall(r"#[0-9a-fA-F]{3,8}\b", content)
    findings = []
    if hex_matches:
        findings.append(
            f"[{severity_prefix}] {len(hex_matches)} hardcoded hex color literal(s) in {target} while "
            f".pcp/design_system.md has established color tokens — consider reusing "
            f"those instead. Examples: {', '.join(hex_matches[:5])}"
            + (f" (screen #{prior_screens + 1} against an already-established system)" if established else "")
        )
    # Positive check (stylelint no-raw-colors posture, 2026-07-17): absence of
    # violations isn't adherence — a UI file that references ZERO named tokens
    # from the established system isn't using it at all. Token vocabulary =
    # CSS custom properties declared in design_system.md. Advisory, same as
    # the hex check; "token systems erode within weeks without hard gates" is
    # the eventual argument for upgrading this, measured first.
    declared_tokens = set(re.findall(r"--[\w-]{3,}", design_system_path.read_text()))
    if declared_tokens and not any(t in content for t in declared_tokens):
        findings.append(
            f"[{severity_prefix}] {target} references none of the {len(declared_tokens)} named design-system "
            "tokens (--*) declared in .pcp/design_system.md — the screen may be styled "
            "outside the system entirely"
            + (f" (screen #{prior_screens + 1} against an already-established system)" if established else "")
        )
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "design-consistency",
        "\n".join(findings) if findings else "no hardcoded colors found",
    )
    _qa_record(
        pcp_dir, ctx, "design-consistency", findings, control_id="CTRL-013", tool="regex",
        evidence_path=evidence_path,
    )
    if findings:
        console.print(f"[yellow]Design consistency (advisory):[/yellow] {findings[0]}")


def _run_a11y_check(pcp_dir: Path, criterion: dict, ctx: dict) -> None:
    """PCP Design lifecycle, stage 4 addendum. Advisory only -- never
    returned into block_findings, same posture as _run_design_consistency_check.
    Deterministic WCAG scan (axe-core via npx, uat.check_axe) against a
    UI-facing criterion's declared url. Only fires when both hold -- most
    criteria have no url at all, and this can't scan a page it can't reach.
    CTRL-022."""
    if not _is_ui_facing_criterion(criterion):
        return
    url = criterion.get("url")
    if not url:
        _qa_record(pcp_dir, ctx, "a11y", [], control_id="CTRL-022", tool=None)
        return
    ok, detail = uat.check_axe(url)
    if ok is None:
        # npx not on PATH -- "could not check", not "failed" (see uat.check_axe).
        _qa_record(pcp_dir, ctx, "a11y", [], control_id="CTRL-022", tool=None)
        return
    findings = [] if ok else [detail]
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "a11y", detail,
    )
    _qa_record(pcp_dir, ctx, "a11y", findings, control_id="CTRL-022", tool="axe-core", evidence_path=evidence_path)
    if findings:
        console.print(f"[yellow]Accessibility (advisory):[/yellow] {detail.splitlines()[0][:200]}")


def _run_visual_quality_check(pcp_dir: Path, project_root: Path, criterion: dict, ctx: dict) -> None:
    """PCP Design lifecycle, stage 4 addendum. Advisory only -- never
    returned into block_findings. Checklist-anchored VLM judge
    (uat.check_visual_quality) over a fresh screenshot of a UI-facing
    criterion's declared url -- research finding behind why this is
    checklist-anchored rather than a freeform "does this look good" prompt:
    a checklist-anchored VLM judge measures ~94% human-correlation vs. ~21%
    for a bare Nielsen-heuristics-style review (ArtifactsBench, 2026).
    Compares against the criterion's own reference_image when declared.
    CTRL-023."""
    if not _is_ui_facing_criterion(criterion):
        return
    url = criterion.get("url")
    if not url:
        _qa_record(pcp_dir, ctx, "visual-quality", [], control_id="CTRL-023", tool=None)
        return

    screenshot_path = pcp_dir / "evidence" / "_visual" / ctx["module"] / f"{ctx['criterion_id']}.png"
    rendered, _render_detail = uat.check_visual(url, screenshot_path)
    if not rendered:
        # Either playwright isn't installed (None) or the page failed to
        # render (False) -- either way there's no screenshot to judge.
        _qa_record(pcp_dir, ctx, "visual-quality", [], control_id="CTRL-023", tool=None)
        return

    reference_image = criterion.get("reference_image")
    reference_path = (project_root / reference_image) if reference_image else None
    ok, detail, items = uat.check_visual_quality(
        screenshot_path, reference_image_path=reference_path, pcp_dir=pcp_dir,
    )
    if ok is None:
        _qa_record(pcp_dir, ctx, "visual-quality", [], control_id="CTRL-023", tool=None)
        return

    findings = [] if ok else [detail]
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "visual-quality",
        json.dumps(items, indent=2) if items else detail,
    )
    _qa_record(
        pcp_dir, ctx, "visual-quality", findings, control_id="CTRL-023", tool="vlm-judge",
        evidence_path=evidence_path,
    )
    if findings:
        console.print(f"[yellow]Visual quality (advisory):[/yellow] {detail[:200]}")


DESIGN_JUSTIFICATION_SYSTEM_PROMPT = (
    "You judge whether a UI criterion's design_justification block reflects real design "
    "thinking or was filled in lazily just to pass validation. You are given the "
    "criterion's own description, an excerpt of the project's design_system.md, and the "
    "submitted checklist_passed/jtbd_framing/deviations_from_system fields. Flag it as NOT "
    "substantive if: checklist_passed is empty or contains junk/placeholder strings; "
    "jtbd_framing is a generic restatement of the criterion description rather than a real "
    "'when a user is X, this lets them Y' conditional; or the whole block reads as "
    "boilerplate. Default to substantive=true when genuinely uncertain -- you are the first "
    "check on this, not the only one; a human still reviews design_audit.md."
)


def _run_design_justification_check(pcp_dir: Path, mod: dict, criterion: dict, ctx: dict) -> list[str]:
    """PCP Design lifecycle stage 4, closing the gap CLAUDE.md names for this
    pillar: design_audit.py's Feature Exposure Ladder (_classify_rung) is
    pure presence/keyword logic -- a checklist_passed full of junk strings or
    a jtbd_framing sentence that merely contains the word "when" anywhere
    still classifies as rung 3/4. That's a passive rollup computed after the
    fact, not enforcement. This is the active check during the build itself:
    same llm.call_json + _verify_block_findings adversarial pattern as
    _run_architect_review/_run_gate_check, and findings BLOCK the criterion
    the same way -- a lazily filled design_justification is exactly the
    "structural-forcing" mechanism CLAUDE.md flags as still missing here.

    Re-reads acceptance.yaml fresh rather than trusting the `criterion` dict
    passed in, which is the pre-attempt snapshot from before the coding
    agent ran -- design_justification is written BY the agent during this
    attempt, so the caller's copy is always stale for this field."""
    if not _is_ui_facing_criterion(criterion):
        return []

    acc_data = load_yaml(mod["acc_path"])
    fresh = next((c for c in acc_data.get("criteria", []) if c["id"] == criterion["id"]), None)
    dj = (fresh or {}).get("design_justification")
    if not dj:
        return []  # rung 1 (Built, Hidden) -- design_audit.py's rollup already surfaces this

    design_system = (pcp_dir / "design_system.md").read_text() if (pcp_dir / "design_system.md").exists() else ""
    prompt = (
        f"## Criterion\n{criterion.get('description', '')}\n\n"
        f"## design_system.md excerpt\n{design_system[:3000]}\n\n"
        f"## design_justification submitted\n"
        f"checklist_passed: {dj.get('checklist_passed')}\n"
        f"jtbd_framing: {dj.get('jtbd_framing')}\n"
        f"deviations_from_system: {dj.get('deviations_from_system')}\n\n"
        '## Respond with JSON only\n'
        '{"substantive": true, "reason": "..."}'
    )
    try:
        res, meta = llm.call_json(
            DESIGN_JUSTIFICATION_SYSTEM_PROMPT, prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir,
            command="build-design-justification", return_meta=True,
        )
    except Exception as e:
        console.print(f"[yellow]Warning: design_justification check call failed: {e}[/yellow]")
        _qa_record(pcp_dir, ctx, "design-justification", [f"call failed: {e}"], control_id="CTRL-015", result="error")
        return []

    findings = []
    if not res.get("substantive", True):
        findings.append(
            f"design_justification for {criterion['id']} reads as lazily filled, not real "
            f"design thinking: {res.get('reason', '')}"
        )
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "design-justification", json.dumps(res, indent=2),
    )
    # _verify_block_findings' first positional param is normally a code diff to
    # ground findings against -- repurposed here as the submitted justification
    # block itself, since that (not a code diff) is what this finding is about.
    kept, _dropped = _verify_block_findings(
        pcp_dir, json.dumps(dj), findings, ctx, "design-justification", "CTRL-015",
    )
    _qa_record(pcp_dir, ctx, "design-justification", kept, meta, control_id="CTRL-015", evidence_path=evidence_path)
    return kept


_CUSTOMIZATION_SIGNAL_KEYWORDS = (
    "setting", "settings", "preference", "preferences", "config", "configure",
    "configuration", "customiz", "personaliz", "toggle", "user_config", "userconfig",
)


def _run_customization_check(pcp_dir: Path, mod: dict, criterion: dict, ctx: dict) -> None:
    """CTRL-026 -- deterministic structural check for design_justification.
    customizable, same posture as CTRL-017's build_vs_buy placeholder check:
    a declared customizable=true should show SOME settings-shaped signal in
    the criterion's own target file, or the declaration reads the same way
    an empty design_justification does -- a claim with nothing behind it.
    Deterministic keyword scan, not a semantic judge call: whether a feature
    is "really" customizable in a way that matters to a user is exactly the
    kind of judgment call CTRL-015's LLM check already makes on the whole
    design_justification block; this only catches the cheap, structural
    failure mode (true declared, zero corroborating signal anywhere).

    Advisory only -- never returned into block_findings, same posture as
    _run_design_consistency_check. Re-reads acceptance.yaml fresh since
    design_justification is written by the coding agent during this attempt."""
    if not _is_ui_facing_criterion(criterion):
        return

    acc_data = load_yaml(mod["acc_path"])
    fresh = next((c for c in acc_data.get("criteria", []) if c["id"] == criterion["id"]), None)
    dj = (fresh or {}).get("design_justification") or {}
    if not dj.get("customizable"):
        _qa_record(pcp_dir, ctx, "customization", [], control_id="CTRL-026", tool=None)
        return

    findings = []
    notes = (dj.get("customization_notes") or "").strip()
    if not notes or len(notes.split()) < 3:
        findings.append(
            f"{criterion['id']} declares customizable=true but customization_notes is "
            f"empty or trivially short ({notes!r}) -- what's actually configurable?"
        )

    target = criterion.get("target")
    target_path = (pcp_dir.parent / target) if target else None
    if target_path and target_path.is_file():
        content = target_path.read_text(errors="replace").lower()
        if not any(k in content for k in _CUSTOMIZATION_SIGNAL_KEYWORDS):
            findings.append(
                f"{criterion['id']} declares customizable=true but {target} shows no "
                "settings/preference/config-shaped signal -- the declaration may be aspirational, not built yet"
            )

    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "customization",
        "\n".join(findings) if findings else f"customizable=true, notes={notes!r}",
    )
    _qa_record(
        pcp_dir, ctx, "customization", findings, control_id="CTRL-026", tool="keyword-scan",
        evidence_path=evidence_path,
    )
    if findings:
        console.print(f"[yellow]Customization check (advisory):[/yellow] {findings[0]}")


_BVB_PLACEHOLDER_PHRASES = frozenset({
    "not specified", "not specified by generator", "todo", "tbd", "n/a", "na",
    "placeholder", "reason", "why this decision", "why this decision, one sentence",
    "one sentence rationale", "one-sentence rationale", "...", "x", "-", "unspecified",
    "not specified by generator -- coerced placeholder, review before treating as a real decision.",
})
_BVB_MIN_WORDS = 4


def _run_build_vs_buy_justification_check(pcp_dir: Path, mod: dict, criterion: dict, ctx: dict) -> list[str]:
    """Structural-forcing for build_vs_buy, same enforcement posture
    design_justification just got (CTRL-015) -- CLAUDE.md names this exact
    gap: build_vs_buy's rationale field is schema-required (must be present)
    but never checked for substance, so "x" or the literal unfilled prompt
    template text passes validation as a real decision.

    Deterministic, NOT an LLM judge call -- unlike design_justification
    (fires only for the UI-facing subset of criteria), build_vs_buy is
    required on EVERY criterion, so an LLM call here on every attempt of
    every criterion would be a real Token Discipline violation for a field
    that mostly just needs a placeholder-text check, not genuine semantic
    judgment. Same placeholder-rejection posture bypass_approval.rego
    already established for bypass reasons (see policy.py), reimplemented
    here in plain Python so it works with zero OPA setup -- build_vs_buy
    validation can't depend on an optional external tool being installed.

    Re-reads acceptance.yaml fresh for the same reason
    _run_design_justification_check does: build_vs_buy can be touched by
    the coding agent during this attempt, so the pre-attempt `criterion`
    snapshot is stale for this field."""
    acc_data = load_yaml(mod["acc_path"])
    fresh = next((c for c in acc_data.get("criteria", []) if c["id"] == criterion["id"]), None)
    bvb = (fresh or {}).get("build_vs_buy") or {}
    decision = bvb.get("decision")
    rationale = (bvb.get("rationale") or "").strip()

    findings = []
    if decision and decision != "not_applicable":
        normalized = rationale.lower().rstrip(".")
        word_count = len(rationale.split())
        if not rationale or normalized in _BVB_PLACEHOLDER_PHRASES or word_count < _BVB_MIN_WORDS:
            findings.append(
                f"build_vs_buy rationale for {criterion['id']} reads as a placeholder, not a "
                f"real decision: '{rationale or '(empty)'}'"
            )

    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "build-vs-buy-justification",
        rationale or "(empty)",
    )
    _qa_record(
        pcp_dir, ctx, "build-vs-buy-justification", findings, control_id="CTRL-017",
        tool="regex", evidence_path=evidence_path,
    )
    return findings


_TEST_PATH_SEGMENTS = ("tests", "test", "__tests__", "spec", "specs")


def _is_test_file(path: str) -> bool:
    parts = Path(path).parts
    if any(seg in _TEST_PATH_SEGMENTS for seg in parts[:-1]):
        return True
    name = Path(path).name.lower()
    return (
        name.startswith("test_") or name == "conftest.py"
        or "_test." in name or ".test." in name or ".spec." in name
    )


def _scope_mode() -> str:
    """PCP_BUILD_SCOPE_MODE: warn (default) | block | off. Ships warn-first
    deliberately -- the same L1-report-only-before-L2-enforcement rollout
    discipline PCP recommends for any new automated gate: measure the
    false-positive rate on real builds (legit cross-cutting writes like a
    module registry exist) before letting it cost retry attempts."""
    mode = os.environ.get("PCP_BUILD_SCOPE_MODE", "warn").lower()
    return mode if mode in ("warn", "block", "off") else "warn"


def _scope_allowlist_violations(mod: dict, criterion: dict, changed_files: list[str]) -> list[str]:
    """Deterministic over-reach check (no LLM): which changed files fall
    outside what this criterion could legitimately touch? Allowed:
    - any `target` file declared by ANY criterion in this module (the module's
      own declared surface, not just this one criterion's file)
    - anything under .pcp/strategy/modules/<module>/ (the agent legitimately
      writes design_justification back into its own acceptance.yaml)
    - .pcp/design_system.md (first UI criterion establishes it)
    - test files (TDD is mandatory -- tests are always in scope)
    """
    module_name = mod["name"]
    try:
        all_criteria = load_yaml(mod["acc_path"]).get("criteria", [])
    except Exception:
        all_criteria = mod.get("pending_criteria", [])
    declared_targets = {c.get("target") for c in all_criteria if c.get("target")}
    if criterion.get("target"):
        declared_targets.add(criterion["target"])
    module_prefix = f".pcp/strategy/modules/{module_name}/"

    violations = []
    for f in changed_files:
        norm = f.replace("\\", "/").removeprefix("./")
        if norm in declared_targets:
            continue
        if norm.startswith(module_prefix) or norm == ".pcp/design_system.md":
            continue
        if _is_test_file(norm):
            continue
        violations.append(norm)
    return violations


def _run_scope_check(pcp_dir: Path, mod: dict, criterion: dict, changed_files: list[str], ctx: dict) -> list[str]:
    """Over-reach guard (CTRL-018): a criterion agent writing files outside
    its module's declared surface is the "loop touches unrelated code"
    failure mode -- PCP had a denylist (protected_path) but no allowlist
    until now. Warn-only by default (see _scope_mode); PCP_BUILD_SCOPE_MODE=
    block returns the finding into block_findings so it costs the attempt."""
    mode = _scope_mode()
    if mode == "off":
        # Disabled by a human -- still visible in the audit trail as skipped,
        # never silently indistinguishable from "ran clean".
        _qa_record(pcp_dir, ctx, "build-scope", [], control_id="CTRL-018", result="skipped")
        return []

    out_of_scope = _scope_allowlist_violations(mod, criterion, changed_files)
    findings = []
    if out_of_scope:
        findings.append(
            f"Scope Guard [CTRL-018]: agent modified {len(out_of_scope)} file(s) outside "
            f"module '{mod['name']}'s declared surface (criterion targets, module spec dir, "
            f"tests): {', '.join(out_of_scope[:8])}"
            + (" …" if len(out_of_scope) > 8 else "")
        )
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "build-scope",
        "\n".join(out_of_scope) if out_of_scope else "all changed files within declared scope",
    )
    # In warn mode a finding here does NOT block, so recording it as `block`
    # makes the audit trail claim something that never happened. Measured on
    # ontology-foundry 2026-07-30: 110 of the project's 259 `block` records were
    # this check in warn mode -- **42.5% of every block PCP had ever recorded
    # there never blocked anything**, so any provenance or block-rate reading of
    # that project was wrong by nearly half. `advisory` already exists as a
    # result value for exactly this (CTRL-025/030/033/036 use it); this check
    # simply wasn't using it. Same invariant _qa_record and _wave_record already
    # state: a result must describe what happened, not what the check found.
    _qa_record(
        pcp_dir, ctx, "build-scope", findings, control_id="CTRL-018", tool="git-diff",
        evidence_path=evidence_path,
        result=("advisory" if findings and mode == "warn" else None),
    )
    if findings and mode == "warn":
        console.print(f"[yellow]Scope guard (advisory):[/yellow] {findings[0]}")
        return []
    return findings


def _run_wave_contract_completeness_check(pcp_dir: Path, wave_number: int) -> list[str]:
    """CTRL-033, ADVISORY, deterministic, project-wide (not per-module --
    ci_rules.yaml is one file). ABC contract-shape reference pattern
    (arXiv:2602.22302, see docs/research-rigidity-vs-reliability-2026-07.md
    and _apply_rule_recovery's own docstring): a hard_block rule with no
    `contract` block at all has no declared recovery plan beyond the flat
    binary severity gate -- same "declared-but-not-enforced-yet" posture
    CTRL-019 already uses for logic_tier presence. Advisory, never blocks;
    a rule without a contract block behaves exactly as it always has."""
    ci_rules_path = pcp_dir / "ci_rules.yaml"
    if not ci_rules_path.exists():
        return []
    try:
        data = load_yaml(ci_rules_path)
    except Exception:
        return []
    findings = [
        f"Rule [{r.get('id')}] '{r.get('name')}' is hard_block with no declared contract "
        "(preconditions/invariants/recovery) -- relies on the flat severity gate only"
        for r in data.get("rules", []) or []
        if r.get("severity") == "hard_block" and not r.get("contract")
    ]
    _wave_record(pcp_dir, wave_number, "contract-completeness", "CTRL-033", findings,
                 files=["ci_rules.yaml"], result="pass")
    for f in findings:
        console.print(f"[yellow]Contract completeness (advisory):[/yellow] {f}")
    return findings


def _run_wave_narrative_lint_check(pcp_dir: Path, wave_number: int) -> list[str]:
    """CTRL-036, ADVISORY, project-wide (not per-module — CLAUDE.md-family
    files aren't scoped to one module). See narrative_lint.py's module
    docstring for the fleet evidence (2026-07-24 context-hygiene pass):
    narrative prose in CLAUDE.md drifted from tracked state 3-for-3 in
    projects checked, undetected by every other gate in this catalog
    because they all validate code against spec, never free-text prose
    against current_state.md/architecture.md. Deterministic sub-checks
    (stale dates, missing referenced files) plus one batched judge call
    for semantic contradiction — same rung-6 posture as CTRL-020."""
    result = narrative_lint.run(pcp_dir)
    findings = result["stale_dates"] + result["missing_files"] + result["contradictions"]
    _wave_record(pcp_dir, wave_number, "narrative-lint", "CTRL-036", findings,
                 files=result["files_scanned"], result="pass")
    for f in findings:
        console.print(f"[yellow]Narrative lint (advisory):[/yellow] {f}")
    return findings


def _run_wave_logic_breakdown_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """CTRL-031, ADVISORY, deterministic-only in this pass. module_logic_
    breakdown backlog item 9's verification half: kickoff/pm already
    keyword-check a declared breakdown item against this module's OWN
    criteria descriptions BEFORE build (check_module_logic_breakdown_
    coverage) -- this re-checks AFTER build, against completed criteria's
    actual target-file content: "does code exist that plausibly reflects
    each declared component," not just "did a criterion get worded to
    mention it." Deterministic keyword scan, not the CTRL-015-style LLM
    judge the backlog item originally sketched -- same rung-1-first posture
    every other check in this catalog started with; the semantic half
    (does the code genuinely FULFILL the component, not just mention it)
    stays deferred."""
    from pcp.commands.kickoff import _keyword_miss_check

    findings: list[str] = []
    for mod in wave_modules:
        spec_path = pcp_dir / "strategy" / "modules" / mod["name"] / "spec.yaml"
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not spec_path.exists() or not acc_path.exists():
            continue
        spec = load_yaml(spec_path)
        breakdown = spec.get("module_logic_breakdown") or []
        if not breakdown:
            continue
        acc = load_yaml(acc_path)
        built_text_parts = []
        for c in acc.get("criteria", []):
            if c.get("status") != "complete":
                continue
            built_text_parts.append(c.get("description", ""))
            target = c.get("target")
            target_path = (pcp_dir.parent / target) if target else None
            if target_path and target_path.is_file():
                built_text_parts.append(target_path.read_text(errors="replace"))
        built_text = " ".join(built_text_parts)
        for f in _keyword_miss_check(
            breakdown, built_text, "Logic-breakdown item",
            "any completed criterion's description or target file",
        ):
            findings.append(f"{mod['name']}: {f}")

    _wave_record(pcp_dir, wave_number, "logic-breakdown", "CTRL-031", findings,
                 files=[m["name"] for m in wave_modules], result="pass")
    for f in findings:
        console.print(f"[yellow]Logic-breakdown check (advisory):[/yellow] {f}")
    return findings


_LAZY_MARKER_PATTERN = re.compile(
    r"\b(TODO|FIXME|XXX|HACK)\b|"
    r"\bnot\s+(?:yet\s+)?implement(?:ed)?\b|\bplaceholder\b|\bcoming\s+soon\b",
    re.IGNORECASE,
)
# def foo(...):\n    pass  (or ... / bare docstring only) -- a stub body,
# not necessarily wrong (abstract methods do this legitimately) but worth
# a glance when it shows up in a criterion's own newly-changed lines.
_STUB_BODY_PATTERN = re.compile(
    r"^\s*def\s+\w+\([^)]*\)[^\n:]*:\s*\n\s*(pass|\.\.\.)\s*$", re.MULTILINE,
)
_LAZY_MARKER_MAX_CHARS = 20_000  # skip pathologically large generated/vendored files


def _run_lazy_marker_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> None:
    """Generic lazy-marker scan (lazy-agent backlog item 3, 2026-07-20).
    PCP previously only checked for placeholder text narrowly, inside
    build_vs_buy/design_justification's own free-text fields (CTRL-017/015).
    This is the general form: a deterministic scan of ALL changed code for
    TODO/FIXME/placeholder-style markers and stub function bodies -- a cheap,
    non-semantic signal that a criterion may have been marked complete over
    unfinished work.

    Advisory only, never blocks -- these markers are not proof of laziness
    (a TODO can be a legitimate forward-looking note, a stub can be a real
    abstract method); this surfaces the count/location for a human to judge,
    same posture as _run_design_consistency_check."""
    findings = []
    for f in changed_files:
        if _is_test_file(f):
            continue
        path = project_root / f
        if not path.is_file():
            continue
        try:
            content = path.read_text(errors="replace")
        except OSError:
            continue
        if len(content) > _LAZY_MARKER_MAX_CHARS:
            continue
        markers = _LAZY_MARKER_PATTERN.findall(content)
        stub_bodies = _STUB_BODY_PATTERN.findall(content)
        if markers:
            findings.append(f"{f}: {len(markers)} lazy-marker hit(s) ({', '.join(sorted(set(m.upper() for m in markers if m))[:5])})")
        if stub_bodies:
            findings.append(f"{f}: {len(stub_bodies)} stub function body/bodies (pass/... only)")

    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], ctx["attempt"], "lazy-marker",
        "\n".join(findings) if findings else "no lazy markers found in changed files",
    )
    _qa_record(
        pcp_dir, ctx, "lazy-marker", findings, control_id="CTRL-029", tool="regex",
        evidence_path=evidence_path,
    )
    if findings:
        console.print(f"[yellow]Lazy-marker scan (advisory):[/yellow] {findings[0]}")


# Mechanism-signature libraries per rung, for the POSITIVE tier check
# (CTRL-019). Import-name based, so the same caveat as CTRL-016 applies —
# package names differ from import names — but these are the import names
# themselves, curated per rung. Rung 5 additionally matches stdlib
# `lru_cache`/`cache` decorators by content, since caching legitimately
# needs no third-party dependency.
_TIER_MECHANISM_LIBS: dict[int, set[str]] = {
    2: {"ortools", "pulp", "cvxpy", "z3", "pyomo", "mip", "scipy"},
    3: {"sklearn", "xgboost", "lightgbm", "catboost", "torch", "tensorflow", "statsmodels", "prophet"},
    4: {"chromadb", "faiss", "qdrant_client", "weaviate", "pinecone", "semantic_router", "rank_bm25", "whoosh", "elasticsearch", "opensearchpy"},
    5: {"gptcache", "redis", "diskcache", "cachetools", "memcache", "pymemcache"},
}

_RUNG5_STDLIB_CACHE_PATTERN = re.compile(r"\blru_cache\b|\bfunctools\.cache\b")

# Judgment-shaped verbs in a criterion description are a cheap contradiction
# signal against a declared rung 1 ("no judgment, fixed conditions").
_JUDGMENT_KEYWORDS = (
    "recommend", "summarize", "summarise", "classify sentiment", "interpret",
    "understand intent", "natural language", "judge", "assess quality", "generate text",
)


def _run_wave_tier_presence_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """7th wave-merge sub-check, CTRL-019, ADVISORY. CTRL-014 checks the
    NEGATIVE for rungs <=5 (no LLM SDK may appear); this checks the POSITIVE
    for rungs 2-5: the declared mechanism should be visible — a rung-2
    criterion whose target imports no solver, a rung-4 with no retrieval
    dependency, a rung-5 with no cache layer is likely a tier declared but
    not actually implemented AT that tier. Advisory because the mechanism may
    legitimately live in a shared helper module the target imports — measure
    the false-positive rate before this can earn hard-block status."""
    project_root = pcp_dir.parent
    findings: list[str] = []
    checked_files: list[str] = []

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete":
                continue
            tier = c.get("logic_tier")
            target = c.get("target")
            if tier not in _TIER_MECHANISM_LIBS or not target:
                continue
            full_path = project_root / target
            if not full_path.exists() or not full_path.is_file():
                continue
            checked_files.append(target)
            imports = _external_python_imports(full_path, project_root)
            expected = _TIER_MECHANISM_LIBS[tier]
            present = bool(imports & expected)
            if not present and tier == 5:
                try:
                    present = bool(_RUNG5_STDLIB_CACHE_PATTERN.search(full_path.read_text(errors="replace")))
                except OSError:
                    present = False
            if not present:
                findings.append(
                    f"Tier presence (advisory): '{mod['name']}/{c['id']}' declares logic_tier={tier} "
                    f"but {target} shows none of that rung's mechanism signatures "
                    f"({', '.join(sorted(expected)[:4])}…) — tier may be declared but not implemented"
                )

    _wave_record(pcp_dir, wave_number, "tier-presence", "CTRL-019", findings,
                 files=checked_files, result="pass")
    for f in findings:
        console.print(f"[yellow]{f}[/yellow]")
    return findings


def _nav_depth_threshold() -> int:
    return int(os.environ.get("PCP_NAV_DEPTH_THRESHOLD", "3"))


def _run_wave_nav_depth_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """CTRL-025, ADVISORY. nav_depth is self-declared (like logic_tier/
    build_vs_buy), not computed from a real routing graph -- per-framework
    route parsing (React Router, Next.js file routes, Vue Router, ...) is a
    bigger build than a single audit field earns on its own. This is the
    audit half: flags UI-facing completed criteria missing the field
    entirely (same "declared-but-absent is itself a finding" posture as
    design_justification), and ones declaring a value past
    PCP_NAV_DEPTH_THRESHOLD (default 3, the classic UX heuristic)."""
    findings: list[str] = []
    checked: list[str] = []

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete" or not _is_ui_facing_criterion(c):
                continue
            checked.append(f"{mod['name']}/{c['id']}")
            depth = c.get("nav_depth")
            if depth is None:
                findings.append(
                    f"Nav depth (advisory): '{mod['name']}/{c['id']}' has no nav_depth declared — "
                    "how many clicks from the app entry point does this feature take to reach?"
                )
            elif depth > _nav_depth_threshold():
                findings.append(
                    f"Nav depth (advisory): '{mod['name']}/{c['id']}' declares nav_depth={depth}, "
                    f"past the {_nav_depth_threshold()}-click threshold — consider surfacing it closer to entry"
                )

    _wave_record(pcp_dir, wave_number, "nav-depth", "CTRL-025", findings, files=checked, result="pass")
    for f in findings:
        console.print(f"[yellow]{f}[/yellow]")
    return findings


def _run_wave_menu_bar_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """CTRL-027, ADVISORY, desktop_app archetype only. Stays completely
    inert -- never even records a telemetry entry -- unless a human has
    explicitly set ui_archetype: desktop_app in .pcp/design_conventions.yaml
    (default web_app). A File/Edit/View/Help-style top menu bar is a
    desktop-app convention, not a universal one; running this
    unconditionally on every project would false-positive on every
    dashboard/SaaS-shaped product PCP builds."""
    conventions_path = pcp_dir / "design_conventions.yaml"
    if not conventions_path.exists():
        return []
    try:
        conventions = load_yaml(conventions_path) or {}
    except Exception:
        return []
    if conventions.get("ui_archetype") != "desktop_app":
        return []
    required = (conventions.get("top_menu_bar") or {}).get("required_menus") or ["File", "Edit", "View", "Help"]

    project_root = pcp_dir.parent
    found_labels: set[str] = set()
    checked: list[str] = []
    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete" or not _is_ui_facing_criterion(c):
                continue
            target = c.get("target")
            if not target:
                continue
            full_path = project_root / target
            if not full_path.is_file():
                continue
            checked.append(target)
            content = full_path.read_text(errors="replace")
            for label in required:
                if label in content:
                    found_labels.add(label)

    missing = [m for m in required if m not in found_labels]
    findings = []
    if missing and checked:
        findings.append(
            f"Top menu bar (advisory): ui_archetype=desktop_app declares required menus "
            f"{required}, but {', '.join(missing)} not found in any scanned UI-facing target file"
        )
    _wave_record(pcp_dir, wave_number, "menu-bar", "CTRL-027", findings, files=checked, result="pass")
    for f in findings:
        console.print(f"[yellow]{f}[/yellow]")
    return findings


def _run_wave_ui_kit_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """CTRL-028, ADVISORY. Stays completely inert (no telemetry record at
    all) unless .pcp/ui_kit_recipes.yaml exists -- same posture as CTRL-027's
    ui_archetype gate. Two checks, both deterministic substring matches, no
    LLM:

    1. Recipe completeness: a criterion declaring screen_archetypes should
       show, among its own ui_organisms, the organisms that archetype's
       recipe requires. Catches "declared dashboard, didn't include a
       chart-panel or data-table" -- a criterion claiming an archetype
       without actually building what that archetype needs.

    2. Import verification: a declared ui_organism should have a matching
       import in the criterion's own target file, per the recipe's
       organism -> import_path_hint mapping. This is the whole point of
       vendoring real component code (shadcn/ui) instead of prose guidance
       -- usage becomes checkable the same way CTRL-019 already checks
       logic_tier mechanism presence via import scanning, not a claim taken
       on faith.

    Both findings are advisory -- an organism can legitimately come from a
    different import path (a re-exported wrapper, a renamed local alias),
    so this is a signal for review, not proof of non-use."""
    recipes_path = pcp_dir / "ui_kit_recipes.yaml"
    if not recipes_path.exists():
        return []
    try:
        recipes = load_yaml(recipes_path) or {}
    except Exception:
        return []
    organism_map = recipes.get("organisms") or {}
    archetype_map = recipes.get("archetypes") or {}

    project_root = pcp_dir.parent
    findings: list[str] = []
    checked: list[str] = []

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete" or not _is_ui_facing_criterion(c):
                continue
            declared_organisms = set(c.get("ui_organisms") or [])
            archetypes = c.get("screen_archetypes") or []

            for archetype in archetypes:
                required = set(archetype_map.get(archetype) or [])
                missing_for_archetype = required - declared_organisms
                if missing_for_archetype:
                    findings.append(
                        f"UI kit (advisory): '{mod['name']}/{c['id']}' declares screen_archetypes="
                        f"[{archetype}] but its ui_organisms is missing {sorted(missing_for_archetype)} "
                        "from that archetype's recipe"
                    )

            target = c.get("target")
            if not target or not declared_organisms:
                continue
            full_path = project_root / target
            if not full_path.is_file():
                continue
            checked.append(target)
            content = full_path.read_text(errors="replace")
            for organism in declared_organisms:
                hint = (organism_map.get(organism) or {}).get("import_path_hint")
                if hint and hint not in content:
                    findings.append(
                        f"UI kit (advisory): '{mod['name']}/{c['id']}' declares ui_organisms "
                        f"including '{organism}' but {target} shows no import matching "
                        f"'{hint}' -- declaration may be unverified"
                    )

    _wave_record(pcp_dir, wave_number, "ui-kit", "CTRL-028", findings, files=checked, result="pass")
    for f in findings:
        console.print(f"[yellow]{f}[/yellow]")
    return findings


RUNG_NECESSITY_SYSTEM_PROMPT = (
    "You audit logic-tier declarations for over-use of LLM reasoning. For each numbered "
    "criterion (all declared rung 6 = deep-think LLM, last resort), answer: could a CHEAPER "
    "rung correctly make this decision — 1 fixed rules/lookup, 2 solver, 3 trained model, "
    "4 retrieval over a bounded corpus, 5 cached replay? The rung-6 test is: would two "
    "competent humans reasonably disagree on the correct output? If they would NOT (the "
    "answer is mechanically derivable), rung 6 is over-declared. Respond JSON only: "
    '{"verdicts": [{"index": 0, "over_declared": false, "cheaper_rung": null, "reason": "..."}]} '
    "— one entry per criterion, in order. Default over_declared=false when genuinely uncertain."
)


def _run_wave_rung_necessity_check(pcp_dir: Path, wave_modules: list[dict], wave_number: int) -> list[str]:
    """8th wave-merge sub-check, CTRL-020, ADVISORY — the deferred "Decision
    Integrity" half: nothing previously challenged a criterion lazily
    declared rung 6 that a truth table could serve. Two layers, cheapest
    first:
    - Deterministic: rung-1 declarations whose description contains
      judgment-shaped language (summarize/recommend/interpret…) — a
      contradiction needing zero LLM.
    - LLM (ONE batched Haiku call per wave, rung-6 criteria only — Token
      Discipline): "did this genuinely need rung 6" is the one irreducibly
      semantic gate in the ladder, so it gets the same judge treatment as
      coverage_score, advisory + recorded, never trusted blindly and never
      blocking. Surfaced in architecture_justification via telemetry."""
    findings: list[str] = []
    rung6: list[tuple[str, str, str]] = []  # (module, id, description)

    for mod in wave_modules:
        acc_path = pcp_dir / "strategy" / "modules" / mod["name"] / "acceptance.yaml"
        if not acc_path.exists():
            continue
        acc = load_yaml(acc_path)
        for c in acc.get("criteria", []):
            if c.get("status") != "complete":
                continue
            tier = c.get("logic_tier")
            desc = c.get("description", "")
            if tier == 1:
                hits = [k for k in _JUDGMENT_KEYWORDS if k in desc.lower()]
                if hits:
                    findings.append(
                        f"Rung necessity (advisory): '{mod['name']}/{c['id']}' declares logic_tier=1 "
                        f"(fixed rules, no judgment) but its description contains judgment-shaped "
                        f"language ({hits[0]!r}) — tier may be under-declared"
                    )
            elif tier == 6:
                rung6.append((mod["name"], c["id"], desc))

    if rung6:
        numbered = "\n".join(f"[{i}] {m}/{cid}: {desc}" for i, (m, cid, desc) in enumerate(rung6))
        try:
            res = llm.call_json(
                RUNG_NECESSITY_SYSTEM_PROMPT, numbered, model=llm.JUDGE_MODEL,
                pcp_dir=pcp_dir, command="wave-rung-necessity",
            )
            for v in res.get("verdicts", []):
                if isinstance(v, dict) and v.get("over_declared"):
                    i = v.get("index")
                    if isinstance(i, int) and 0 <= i < len(rung6):
                        m, cid, _ = rung6[i]
                        findings.append(
                            f"Rung necessity (advisory): '{m}/{cid}' declared rung 6 but judge "
                            f"assesses rung {v.get('cheaper_rung')} could serve: {v.get('reason', '')[:200]}"
                        )
        except Exception as e:
            console.print(f"[dim]Rung-necessity judge call failed (advisory check skipped): {e}[/dim]")

    _wave_record(pcp_dir, wave_number, "rung-necessity", "CTRL-020", findings,
                 files=[], result="pass")
    for f in findings:
        console.print(f"[yellow]{f}[/yellow]")
    return findings


def _record_escalation(pcp_dir: Path, module_name: str, criterion_id: str, block_findings: list[str]) -> None:
    """Route this criterion's final-attempt failure through OPA's escalation
    policy (.pcp/policies/escalation.rego) -- advisory only, doesn't change
    control flow (a 3rd-attempt failure already stops the build and hands
    back to a human either way). Confidence is a simple proxy: how many
    distinct gate categories still had violations on the last attempt.
    high_stakes fires on any SEC_* (secrets/eval/sql-injection) finding --
    those should never be treated as routine, low-stakes retries.

    Degrades silently if opa isn't installed or no escalation.rego is
    scaffolded -- this is informational, never a hard dependency on OPA.

    Regardless of OPA availability, the escalation itself is appended to
    .pcp/escalations.yaml -- the staleness watchdog (escalations.find_stale,
    surfaced by `pcp watch` and `pcp status`) needs a ledger that exists on
    every project, not only ones with opa installed. Recording an escalation
    and a human actually seeing it are different facts; the ledger is what
    lets the second one be checked."""
    from pcp import escalations, policy

    escalations.record(pcp_dir, module_name, criterion_id, findings=block_findings)
    gate_categories = 6  # test-suite, lint, sast, layer1, architect-review, gate
    distinct_violations = len(block_findings)
    confidence_score = max(0.0, 1.0 - (distinct_violations / gate_categories))
    high_stakes = any(f.startswith("File Rule [SEC_") or f.startswith("AST Rule [SEC_") for f in block_findings)

    decision = policy.evaluate(
        pcp_dir, "data.pcp.escalation.route",
        {"confidence_score": confidence_score, "high_stakes": high_stakes},
    )
    if not decision.get("available") or decision.get("undefined"):
        return

    route = decision.get("value", "human")
    console.print(
        f"[dim]Escalation policy: route={route} "
        f"(confidence={confidence_score:.2f}, high_stakes={high_stakes})[/dim]"
    )
    with _STATE_LOCK:
        telemetry.record(
            pcp_dir, cycle="qa", cycle_number=None, check="escalation",
            module=module_name, submodule=None, criterion_id=criterion_id,
            files=[], result="pass", errors=[f"route={route}"], error_count=0,
        )


_COMPLEXITY_KEYWORDS = (
    "integrat", "concurren", "parallel", "migrat", "auth", "encrypt", "distributed",
    "real-time", "realtime", "websocket", "transaction", "cache invalidat", "state machine",
)


def _complexity_route(pcp_dir: Path, mod: dict, c: dict) -> tuple[bool, dict]:
    """Deterministic pre-attempt-1 complexity signal (2026-07-17). Routing
    beats cascading — a cascade pays the cheap model's cost BEFORE the
    escalation decision ("Is Escalation Worth It?", arXiv:2605.06350) — but
    PCP has no learned router yet, so this is a rung-1 heuristic: description
    length, complexity keywords, module dependency count, and this module's
    own historical retry rate from telemetry (bandit-ish: only outcomes PCP
    actually observed, the BaRP framing).

    REPORT-FIRST rollout (standing rule): by default this only records what
    it WOULD do (telemetry check="complexity-route", result="pass"); routing
    only takes effect with PCP_COMPLEXITY_ROUTING=1. Returns
    (route_to_escalation_model, signal_dict)."""
    desc = c.get("description", "")
    score = 0.0
    if len(desc) > 200:
        score += 1
    hits = [k for k in _COMPLEXITY_KEYWORDS if k in desc.lower()]
    score += min(len(hits), 3)
    deps = (mod.get("spec") or {}).get("dependencies") or []
    if len(deps) >= 2:
        score += 1
    # historical: this module's build records — retries per criterion
    module_builds = [r for r in telemetry.load(pcp_dir)
                     if r.get("cycle") == "build" and r.get("module") == mod["name"]]
    retries = sum(1 for r in module_builds if (r.get("cycle_number") or 1) > 1)
    if module_builds and retries / max(len(module_builds), 1) > 0.4:
        score += 2
    route = score >= 3
    return route, {"score": score, "keyword_hits": hits, "deps": len(deps),
                   "module_retry_ratio": round(retries / max(len(module_builds), 1), 2) if module_builds else 0.0}


_ARCHITECT_PREFLIGHT_SYSTEM_PROMPT = """\
You are a software architect doing a PRE-IMPLEMENTATION sanity check — no code exists yet for this criterion. \
Review the PLANNED approach (its description, declared logic_tier, declared build_vs_buy decision, and the \
module it belongs to) for genuine red flags before any code is written: a declared logic_tier that contradicts \
the description's own language (e.g. rung 1 "deterministic" but the description asks for judgment/summarization), \
a declared build_vs_buy that conflicts with what the module's dependencies/constraints already establish, or a \
plan that looks structurally unsound given the architecture doc. Do NOT invent hypothetical implementation \
mistakes that haven't happened yet — only flag concerns groundable in the declared fields themselves. \
Output ONLY valid JSON: {"findings": [{"concern": "...", "suggestion": "..."}]}. Empty list if nothing to flag."""


def _run_architect_preflight(pcp_dir: Path, mod: dict, criterion: dict) -> list[str]:
    """Architect pre-flight (swarm-role backlog, 2026-07-20): PCP's existing
    architect-review (_run_architect_review) is POST-HOC only -- it reviews
    the diff after code is written. This is the genuinely new lifecycle
    point the backlog named: a pre-implementation consult, before any code
    exists, for HIGH-RISK criteria only (logic_tier >= 5, or a criterion-
    level build_vs_buy of reuse_whole/fork_adapt -- a real external-
    dependency commitment worth a second look before it's acted on).

    Advisory in this pass, NOT the block_findings channel the backlog
    sketched -- PCP's attempt loop has no separate "submit a plan, then
    code" step (one agent session does both), so wiring this into
    block_findings would mean skipping a whole attempt with no code
    written, a real behavior change to a heavily-relied-on 3-attempt
    contract. Same L1-report-first rollout discipline as every other new
    check in this catalog: advisory now, upgrade to blocking only after a
    measured false-positive rate earns it. Returns lines to inject into the
    criterion's own attempt-1 prompt (empty if not high-risk or nothing to flag)."""
    tier = criterion.get("logic_tier")
    bvb_decision = (criterion.get("build_vs_buy") or {}).get("decision")
    high_risk = (isinstance(tier, int) and tier >= 5) or bvb_decision in ("reuse_whole", "fork_adapt")
    if not high_risk:
        return []

    spec_summary = {
        "module": mod["name"], "description": mod.get("spec", {}).get("description", ""),
        "dependencies": mod.get("spec", {}).get("dependencies", []),
        "constraints": mod.get("spec", {}).get("constraints", []),
    }
    user_prompt = (
        f"Criterion: [{criterion.get('id')}] {criterion.get('description', '')}\n"
        f"Declared logic_tier: {tier}\n"
        f"Declared build_vs_buy: {criterion.get('build_vs_buy')}\n"
        f"Module context: {json.dumps(spec_summary, default=str)}"
    )
    try:
        res = llm.call_json(
            _ARCHITECT_PREFLIGHT_SYSTEM_PROMPT, user_prompt,
            model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="architect-preflight",
        )
    except Exception as e:
        console.print(f"[yellow]Warning: Architect pre-flight call failed: {e}[/yellow]")
        return []

    findings = res.get("findings", []) if isinstance(res, dict) else []
    ctx = {"module": mod["name"], "submodule": None, "criterion_id": criterion.get("id"), "attempt": 0, "files": []}
    rendered = [f"{f.get('concern', '')} — {f.get('suggestion', '')}" for f in findings if f.get("concern")]
    evidence_path = evidence.store(
        pcp_dir, ctx["module"], ctx["criterion_id"], 0, "architect-preflight",
        "\n".join(rendered) if rendered else "no pre-flight concerns",
    )
    _qa_record(
        pcp_dir, ctx, "architect-preflight", rendered, control_id="CTRL-032", tool="judge-model",
        result="pass", evidence_path=evidence_path,
    )
    if rendered:
        console.print(f"[yellow]Architect pre-flight (advisory):[/yellow] {rendered[0]}")
    return rendered


def _run_install_only(
    pcp_dir: Path, project_root: Path, mod: dict, *,
    criterion: dict | None, install_command: str, candidate_desc: str, yes: bool,
    budget: "_BuildBudget",
) -> tuple[bool, list[str]]:
    """Fast path for a human-confirmed direct prior-art match — skip the full
    TDD/architect-review/LLM-gate cycle entirely, just install + verify with
    deterministic checks (full test suite + Layer 1 ci_rules — CTRL-034, no
    LLM calls). criterion=None means module-level (whole module satisfied by
    one dependency, see spec.yaml's install_only). This is never a silent
    skip: declining the approval prompt, or a failed smoke test, both fall
    through to the normal full build path unchanged — the caller decides
    what "fall through" means at its own scope (retry the one criterion, or
    resume the module's normal per-criterion loop)."""
    scope_label = f"{mod['name']}/{criterion['id']}" if criterion else f"{mod['name']} (whole module)"
    console.print(f"\n[bold]Install-only fast path — {scope_label}[/bold]")
    console.print(f"[dim]Candidate:[/dim] {candidate_desc}")
    console.print(f"[dim]Install command:[/dim] {install_command}")

    criterion_id = criterion["id"] if criterion else None
    if not yes:
        if not click.confirm("Confirm this is a direct match and proceed with install-only?", default=False):
            console.print("[yellow]Declined — falling through to full build.[/yellow]")
            log_install_approval(
                pcp_dir, module=mod["name"], criterion_id=criterion_id,
                candidate=candidate_desc, install_command=install_command,
                decision="reject", actor="human",
            )
            return False, ["human declined install-only approval"]
        actor = "human"
    else:
        actor = "yes-flag"

    log_install_approval(
        pcp_dir, module=mod["name"], criterion_id=criterion_id,
        candidate=candidate_desc, install_command=install_command,
        decision="confirm", actor=actor,
    )

    start_ref = _git_head(project_root)
    try:
        result = subprocess.run(
            install_command, shell=True, cwd=project_root,
            capture_output=True, text=True, timeout=_build_agent_timeout_sec(),
        )
    except subprocess.TimeoutExpired:
        return False, [f"install_command timed out after {_build_agent_timeout_sec()}s"]

    changed_files = _get_changed_files_since(project_root, start_ref)
    ctx = {
        "module": mod["name"], "submodule": None,
        "criterion_id": criterion_id or "MODULE",
        "attempt": 1, "files": changed_files,
    }

    if result.returncode != 0:
        errors = [f"install_command failed (exit {result.returncode}): {(result.stderr or '')[-1000:]}"]
        _qa_record(pcp_dir, ctx, "install-only", errors, control_id="CTRL-034", tool="install", result="block")
        console.print(f"[red]Install failed:[/red] {errors[0]}")
        return False, errors

    violations = _run_layer1_check(pcp_dir, project_root, changed_files, ctx)
    violations += _run_test_suite_check(pcp_dir, project_root, ctx)
    # SAST added 2026-07-27. This fast path exists precisely to pull in
    # THIRD-PARTY code on a human's say-so, which makes it the single place a
    # supply-chain problem is most likely to enter — and it was the one path
    # that skipped the secret/SAST scan entirely. The LLM gates are genuinely
    # not worth running here (there is no agent-written diff to review, which
    # is the whole point of the fast path), but a deterministic scan of what
    # the install actually put on disk costs one semgrep run and is exactly
    # the check this path most needs. Cheap, deterministic, no LLM calls —
    # consistent with CTRL-034's "skip the expensive cycle, keep the
    # verification" posture.
    violations += _run_sast_check(pcp_dir, project_root, changed_files, ctx, budget)

    if violations:
        console.print("[red]Install-only smoke test failed — falling through to full build.[/red]")
        _qa_record(pcp_dir, ctx, "install-only", violations, control_id="CTRL-034", tool="install", result="block")
        return False, violations

    _qa_record(pcp_dir, ctx, "install-only", [], control_id="CTRL-034", tool="install", result="pass")
    console.print(f"[green]✓ Install-only fast path passed — {scope_label}[/green]")
    _auto_commit_criterion(project_root, mod["name"], criterion or {"id": "MODULE", "description": candidate_desc})
    return True, []


def _build_one_criterion(
    pcp_dir: Path, project_root: Path, mod: dict, c: dict,
    build_model: str | None, build_model_explicit: bool, budget: "_BuildBudget",
    yes: bool = False,
) -> tuple[bool, list[str]]:
    """Runs the up-to-3-attempt loop for ONE criterion. `project_root` is
    where the coding agent actually runs and where gates are evaluated —
    either the main project root (serial/single-module path) or a per-module
    git worktree (parallel path). Shared-file writes (telemetry, cost ledger,
    capture) always target the real `pcp_dir`, never a worktree copy, and are
    internally guarded by `_STATE_LOCK` (see _qa_record/_log_usage call
    sites) — never held across gate evaluation itself, since the LLM calls
    and test/lint/SAST subprocesses are exactly the work parallelism exists
    to overlap. Returns (success, last block_findings)."""
    if c.get("install_only"):
        install_command = c.get("install_command")
        if not install_command:
            console.print(f"[red]{mod['name']}/{c['id']} declares install_only but has no install_command — falling through to full build.[/red]")
        else:
            candidate_desc = (c.get("build_vs_buy") or {}).get("rationale") or install_command
            ok, findings = _run_install_only(
                pcp_dir, project_root, mod, criterion=c,
                install_command=install_command, candidate_desc=candidate_desc, yes=yes,
                budget=budget,
            )
            if ok:
                return True, []
            # Falls through to the normal full build loop below — a
            # declined approval or failed smoke test is a real signal
            # this wasn't actually a direct match, not a reason to give up.

    feedback = None
    success = False
    block_findings: list[str] = []
    attempt_history: list[str] = []
    agent_session_id = str(uuid.uuid4())
    # Everything the agent does this criterion — committed or not — is
    # measured against this ref, so committing can't hide work from gates.
    criterion_start_ref = _git_head(project_root)

    # run_log bracket — pre/post audit entry, actor="pcp-build-agent" so this
    # is queryable as real pipeline work, distinct from manual/interactive
    # runs bracketed via `pcp run-log`. Never blocks a real build on failure.
    run_log_id = None
    run_log_tokens = {"input": 0, "output": 0, "cache_read": 0, "cost": 0.0}
    run_log_last_checks: list[str] = []
    try:
        run_log_id = run_log.start_run(
            pcp_dir, module=mod["name"], feature=f"{c['id']}: {c.get('description', '')}",
            run_type="dev", actor="pcp-build-agent", model=build_model,
        )
    except Exception as e:
        console.print(f"[dim]run-log start skipped: {e}[/dim]")

    # Complexity routing (report-first; see _complexity_route). Never
    # overrides an explicit human PCP_BUILD_MODEL.
    route_up, route_signal = _complexity_route(pcp_dir, mod, c)
    routing_active = os.environ.get("PCP_COMPLEXITY_ROUTING") == "1"
    with _STATE_LOCK:
        telemetry.record(
            pcp_dir, cycle="qa", cycle_number=0, check="complexity-route", control_id=None,
            module=mod["name"], submodule=None, criterion_id=c["id"], files=[],
            result="pass",
            errors=[f"would_route_to_escalation_model={route_up} active={routing_active} signal={route_signal}"],
            error_count=0,
        )
    if route_up and routing_active and not build_model_explicit:
        console.print(f"[dim]Complexity routing: starting on {llm.ESCALATION_MODEL} (signal {route_signal['score']}).[/dim]")
        build_model = llm.ESCALATION_MODEL

    # Architect pre-flight (swarm-role backlog): one Haiku call, high-risk
    # criteria only, BEFORE any code exists. See _run_architect_preflight's
    # own docstring for why this stays advisory (prompt injection) rather
    # than the block_findings channel in this pass.
    preflight_lines = _run_architect_preflight(pcp_dir, mod, c)

    for attempt in range(1, 4):
        console.print(f"\n[dim]Attempt {attempt}/3 — {mod['name']}/{c['id']}...[/dim]")
        _write_progress(pcp_dir, mod["name"], c["id"], attempt, "coding")

        allowed, spend_reason = spend.check_ceiling(pcp_dir)
        if not allowed:
            console.print(f"[red bold]Project spend ceiling reached:[/red bold] {spend_reason}")
            console.print("[dim]No further agent sessions will be spawned this run.[/dim]")
            raise BudgetExceeded(spend_reason)

        try:
            budget.take_session()
        except BudgetExceeded:
            console.print(
                f"[red bold]Budget circuit breaker: exceeded {budget.max_sessions} agent "
                "sessions this run.[/red bold]"
            )
            console.print("[dim]Override with PCP_MAX_BUILD_SESSIONS=<n> if this build genuinely needs more.[/dim]")
            raise

        # Attempt 1 opens a fresh session; attempt 2 --resumes it (avoids
        # re-exploring the repo — Token Discipline). Attempt 3 (escalation)
        # deliberately does NOT resume: failed-attempt context contaminates
        # retries (CCRM, arXiv:2605.08563 — contaminated-context error rate
        # 7.1x baseline, "clean-restart dominance") — the escalated model gets
        # a FRESH session plus a structured summary of what failed, not the
        # raw failure trajectory (summarize-don't-replay, arXiv:2604.16529).
        if attempt == 1:
            agent_prompt = _build_agent_prompt(pcp_dir, mod["name"], c, mod["spec"])
            if preflight_lines:
                agent_prompt += "\n".join([
                    "",
                    "## Architect pre-flight concerns (advisory, raised before you started — address or explicitly reason past them):",
                    *[f"- {line}" for line in preflight_lines],
                ])
            session_flag = ["--session-id", agent_session_id]
        elif attempt == 2:
            agent_prompt = _build_retry_prompt(feedback)
            session_flag = ["--resume", agent_session_id]
        else:
            escalation_session_id = str(uuid.uuid4())
            agent_prompt = _build_escalation_prompt(pcp_dir, mod["name"], c, mod["spec"], attempt_history)
            session_flag = ["--session-id", escalation_session_id]
            agent_session_id = escalation_session_id

        # Commit-trailer attribution: the installed commit-msg hook stamps
        # PCP-Agent-Session onto any commit made inside this subprocess —
        # set AFTER the escalation branch so attempt 3 carries its own id.
        os.environ["PCP_AGENT_SESSION_ID"] = agent_session_id

        # Escalate to Opus on the final attempt -- two Sonnet attempts already
        # failed, a real complexity signal worth paying up for before handing
        # off to human escalation. Never overrides an explicit human choice:
        # PCP_BUILD_MODEL set on attempt 1 stays in effect on attempt 3 too,
        # rather than silently switching models without being asked.
        attempt_model = build_model
        if attempt == 3 and not build_model_explicit:
            attempt_model = llm.ESCALATION_MODEL

        cmd = [
            _claude_bin(),
            "-p",
            "--permission-mode", "acceptEdits",
            "--output-format", "json",
            "--max-budget-usd", _build_agent_max_budget_usd(),
            *session_flag,
        ]
        if attempt_model:
            cmd += ["--model", attempt_model]

        # Run Claude agent — wall-clock capped. A stuck/looping agent must
        # not be able to run unbounded just because it hasn't returned yet.
        try:
            result = subprocess.run(
                cmd, input=agent_prompt, text=True, capture_output=True,
                cwd=project_root, timeout=_build_agent_timeout_sec(),
            )
        except subprocess.TimeoutExpired:
            timeout_sec = _build_agent_timeout_sec()
            console.print(f"[red]Claude agent timed out after {timeout_sec}s.[/red]")
            feedback = f"Previous attempt exceeded the {timeout_sec}s per-attempt timeout and was killed."
            attempt_history.append(f"Attempt {attempt}: {feedback}")
            continue

        if result.returncode != 0:
            console.print("[red]Claude agent exited with error.[/red]")
            feedback = "Claude CLI agent run failed or exited with non-zero code."
            attempt_history.append(f"Attempt {attempt}: {feedback}")
            continue

        agent_usage = {}
        try:
            envelope = json.loads(result.stdout)
            if envelope.get("is_error"):
                console.print(f"[red]Claude agent reported an error:[/red] {envelope.get('result', '')}")
                feedback = f"Previous attempt errored: {envelope.get('result', '')}"
                attempt_history.append(f"Attempt {attempt}: {feedback[:500]}")
                continue
            with _STATE_LOCK:
                _log_usage(
                    pcp_dir, "build-agent", attempt_model, envelope.get("session_id"),
                    envelope.get("usage", {}), envelope.get("total_cost_usd"),
                )
            budget.add_cost(envelope.get("total_cost_usd"))
            agent_usage = {
                "model": attempt_model or "default",
                "session_id": envelope.get("session_id"),
                "usage": envelope.get("usage", {}),
                "cost_usd": envelope.get("total_cost_usd"),
                "duration_ms": envelope.get("duration_ms"),
            }
            _u = envelope.get("usage", {})
            run_log_tokens["input"] += _u.get("input_tokens", 0) + _u.get("cache_creation_input_tokens", 0)
            run_log_tokens["output"] += _u.get("output_tokens", 0)
            run_log_tokens["cache_read"] += _u.get("cache_read_input_tokens", 0)
            run_log_tokens["cost"] += envelope.get("total_cost_usd") or 0
        except (json.JSONDecodeError, TypeError):
            pass

        # Run checks. PCP's own operational writes (token ledger, telemetry)
        # are not agent work product — never fed to gates or the scope guard.
        changed_files = [
            f for f in _get_changed_files_since(project_root, criterion_start_ref)
            if not _is_pcp_operational(f)
        ]

        if not changed_files:
            console.print("[yellow]No files were modified by the agent (committed or uncommitted).[/yellow]")

        diff = _get_working_diff(project_root, criterion_start_ref)

        lines_added, lines_removed = telemetry.count_diff_lines(diff)
        usage = agent_usage.get("usage", {})
        with _STATE_LOCK:
            telemetry.record(
                pcp_dir,
                cycle="build", cycle_number=attempt,
                module=mod["name"], submodule=None, criterion_id=c["id"],
                files=changed_files, languages=telemetry.infer_languages(changed_files),
                lines_added=lines_added, lines_removed=lines_removed,
                model=agent_usage.get("model"), session_id=agent_usage.get("session_id"),
                token_input=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                token_output=usage.get("output_tokens", 0),
                token_cache_read=usage.get("cache_read_input_tokens", 0),
                cost_usd=agent_usage.get("cost_usd"), duration_ms=agent_usage.get("duration_ms"),
            )

            # Conversational drift capture — classify this agent session's own
            # transcript into business/technical drift. Advisory, never blocks;
            # silently skips if the session transcript can't be located.
            agent_session_id_actual = agent_usage.get("session_id")
            if agent_session_id_actual:
                transcript_path = find_transcript_for_session(agent_session_id_actual)
                if transcript_path:
                    run_capture(
                        pcp_dir, transcript_path,
                        source=f"build:{mod['name']}:{c['id']}",
                        session_id=agent_session_id_actual,
                    )

        # Running gates -- all thirteen checks below are mutually independent
        # (each reads disk/git/subprocess/an LLM call and writes only its own
        # evidence file + a lock-guarded _qa_record/_log_usage call), so they
        # run concurrently rather than one after another. Until 2026-07-18
        # these ran strictly sequentially within one criterion even though
        # nothing here depends on another check's output -- a real dogfood
        # finding (ontology-foundry): with 3 of these being LLM calls and the
        # rest subprocess/network calls, sequential execution was pure wasted
        # wall-clock. The comment this replaced only justified running
        # OUTSIDE _STATE_LOCK for overlap ACROSS concurrently-building
        # modules -- it never actually parallelized the checks WITHIN one
        # criterion, which is what actually happens here now. Each check
        # function's own _qa_record call remains lock-guarded, and
        # llm.client._log_usage (token_ledger.yaml) now has its own lock too
        # (previously unguarded -- fine when only one gate call ever ran at
        # a time, a real race the moment more than one runs concurrently).
        console.print(f"[dim]Evaluating gates ({mod['name']}/{c['id']})...[/dim]")
        _write_progress(pcp_dir, mod["name"], c["id"], attempt, "qa: evaluating gates")
        ctx = {
            "module": mod["name"], "submodule": None, "criterion_id": c["id"],
            "criterion_description": c.get("description", ""),
            "attempt": attempt, "files": changed_files,
        }
        # A criterion gate tests THE BUILT PRODUCT. Nothing here inspects PCP's
        # own paperwork -- declarations about how the code was decided on.
        #
        # Measured on ontology-foundry 2026-07-27, 1,632 gate executions:
        # 35% of them checked declarations rather than the product, and those
        # produced 108 of 187 total blocks -- 58%. Of those, 97 were the scope
        # guard reporting "agent modified N files outside the declared surface"
        # against a surface derived from `target`, which 331 of 382 criteria
        # never declared. PCP was blocking on its own missing metadata, then
        # charging that check to every attempt of every criterion.
        #
        # Removed from this loop: scope (CTRL-018), build_vs_buy justification
        # (CTRL-017), design justification (CTRL-015), customization (CTRL-026).
        # All four grade declaration TEXT or declared file surfaces; none can
        # tell you whether the thing works. If that reporting is wanted it
        # belongs in `pcp audit` over a finished project, not in the build's
        # hot path.
        #
        # What stays is exactly what can fail the product: does it pass its
        # tests, is it clean (lint/SAST/ci_rules), does the diff hold up to
        # review, does the UI actually render and meet a11y, and did the agent
        # leave stubs behind (lazy_marker catches TODO/placeholder bodies
        # shipped as complete -- a real defect, not paperwork).
        gate_calls = {
            "tests": lambda: _run_test_suite_check(pcp_dir, project_root, ctx),
            "lint": lambda: _run_lint_check(pcp_dir, project_root, changed_files, ctx, budget),
            "sast": lambda: _run_sast_check(pcp_dir, project_root, changed_files, ctx, budget),
            "l1": lambda: _run_layer1_check(pcp_dir, project_root, changed_files, ctx),
            "arch": lambda: _run_architect_review(pcp_dir, diff, changed_files, ctx),
            "gate": lambda: _run_gate_check(pcp_dir, diff, ctx),
            "design_consistency": lambda: _run_design_consistency_check(pcp_dir, project_root, c, ctx),
            "a11y": lambda: _run_a11y_check(pcp_dir, c, ctx),
            "visual_quality": lambda: _run_visual_quality_check(pcp_dir, project_root, c, ctx),
            "lazy_marker": lambda: _run_lazy_marker_check(pcp_dir, project_root, changed_files, ctx),
        }
        with ThreadPoolExecutor(max_workers=len(gate_calls)) as pool:
            futures = {name: pool.submit(fn) for name, fn in gate_calls.items()}
            gate_results = {name: f.result() for name, f in futures.items()}
        run_log_last_checks = list(gate_calls.keys())

        test_timed_out = any("timed out" in v.lower() for v in gate_results["tests"])
        if budget.record_test_timeout_signal(test_timed_out):
            console.print(
                f"[red bold]Infra anomaly suspected:[/red bold] the test-suite gate has now "
                f"\"timed out\" on {budget.infra_signal_streak} consecutive attempts. This usually "
                "means the environment is broken (wrong/unreachable DB, a squatted port, a hung "
                "service), not the agent's code -- see the 2026-07-21 ontology-foundry incident. "
                "Verify the environment before trusting further gate results this run."
            )
            from pcp import escalations
            with _STATE_LOCK:
                escalations.record(
                    pcp_dir, mod["name"], c["id"], route="infra-anomaly",
                    findings=[
                        f"{budget.infra_signal_streak} consecutive test-suite gate timeouts across "
                        "criteria -- likely environment/infra issue, not per-criterion agent code "
                        "quality. Check DB/service connectivity and for port conflicts before trusting "
                        "further results this run.",
                    ],
                )

        # Blocking set = product failures only. `scope`, `design_justification`
        # and `bvb_justification` used to block here; see the gate_calls comment
        # above for why declaration-grading no longer stops a build.
        block_findings = (
            gate_results["tests"] + gate_results["lint"] + gate_results["sast"]
            + gate_results["l1"] + gate_results["arch"] + gate_results["gate"]
        )

        if block_findings:
            console.print(f"[red]BLOCKED by quality/architecture gates ({mod['name']}/{c['id']}):[/red]")
            for v in block_findings:
                console.print(f"  ✗ {v}")
            feedback = "\n".join(block_findings)
            attempt_history.append(
                f"Attempt {attempt}: blocked by gates — " + "; ".join(v[:200] for v in block_findings[:5])
            )
        else:
            success = True
            break

    # Unconditional — pass or fail. A criterion that exhausts all 3 attempts
    # still leaves its worktree "for inspection" (never merged to main; only
    # a successful criterion gets merged), but real agent work must not sit
    # as raw uncommitted files that a stale worktree removal could lose —
    # the exact ontology-foundry web-server-A013/14/15 pattern, 2026-07-23.
    _auto_commit_criterion(project_root, mod["name"], c)

    if run_log_id:
        try:
            entry = run_log.end_run(
                pcp_dir, run_log_id, result="success" if success else "failure",
                model=build_model,
                token_input=run_log_tokens["input"], token_output=run_log_tokens["output"],
                token_cache_read=run_log_tokens["cache_read"], cost_usd=run_log_tokens["cost"],
                tests_ran="tests" in run_log_last_checks,
                tests_passed=(success if "tests" in run_log_last_checks else None),
                real_gates_passed=[k for k in run_log_last_checks if k in run_log._DETERMINISTIC_CHECKS],
                llm_judged_gates_passed=[k for k in run_log_last_checks if k in run_log._LLM_JUDGED_CHECKS],
                self_reported_usage=False,
            )
            if entry["anomaly_flags"]:
                console.print(f"[yellow]run-log anomalies ({mod['name']}/{c['id']}):[/yellow] " + "; ".join(entry["anomaly_flags"]))
        except Exception as e:
            console.print(f"[dim]run-log end skipped: {e}[/dim]")

    return success, block_findings


def _mark_criterion_complete(mod: dict, criterion_id: str, verified_by: str = "pcp_build") -> None:
    """No cross-module contention (each module only ever touches its own
    acceptance.yaml), but still guarded for consistency — and, under
    criterion-level parallelism, this same file IS written by concurrent
    threads for different criteria in the same module, so the guard is load-
    bearing there, not just defensive.

    `verified_by` (2026-07-24): the ONLY place a criterion's status ever
    flips to complete through this real gated loop -- stamping it here is
    what makes current_state.md/dashboard able to show "audited complete"
    vs a hand-edited acceptance.yaml (which never touches this function, so
    a manually-flipped criterion simply has no verified_by field at all).
    Closes the "pcp build and a regular build say completed the same way"
    gap named 2026-07-24."""
    with _STATE_LOCK:
        acc_data = load_yaml(mod["acc_path"])
        for crit in acc_data.get("criteria", []):
            if crit["id"] == criterion_id:
                crit["status"] = "complete"
                crit["verified_by"] = verified_by
        mod["acc_path"].write_text(yaml.dump(acc_data, default_flow_style=False))


def _build_module_worker(
    pcp_dir: Path, mod: dict, project_root: Path,
    build_model: str | None, build_model_explicit: bool, budget: "_BuildBudget",
    yes: bool = False,
) -> dict:
    # A malformed spec must fail THIS module, not the run. On 2026-07-27 a build
    # agent hand-edited an acceptance.yaml into invalid YAML and the raw
    # ScannerError ended a run that had already completed two modules. Other
    # modules had nothing to do with that file and should keep going.
    try:
        return _build_module_worker_inner(
            pcp_dir, mod, project_root, build_model, build_model_explicit, budget, yes,
        )
    except MalformedSpecError as exc:
        console.print(f"[red]✗ Module '{mod['name']}' has an unreadable spec:[/red] {exc}")
        return {"module": mod["name"], "success": False,
                "failed_criterion": None, "block_findings": [str(exc)]}


def _build_module_worker_inner(
    pcp_dir: Path, mod: dict, project_root: Path,
    build_model: str | None, build_model_explicit: bool, budget: "_BuildBudget",
    yes: bool = False,
) -> dict:
    """Runs all of one module's pending criteria inside `project_root` (its
    own worktree when building in parallel across modules). Stops at the
    first criterion (or criterion-wave) that fails. Never raises for a
    build/gate failure — only BudgetExceeded propagates, since that's a
    whole-run circuit breaker, not a per-module outcome.

    Sequential by default (`_criteria_parallel_enabled` is False for any
    module where no criterion declares `depends_on`) — the pre-existing,
    unchanged code path. Opt-in criterion-level parallel waves are a
    separate branch below, not a rewrite of the default one."""
    console.print(f"\n[bold]Building Module:[/bold] [cyan]'{mod['name']}'[/cyan] ({len(mod['pending_criteria'])} pending criteria)")

    # Whole-module direct-match fast path (spec.yaml's install_only) — one
    # approval + one install + one smoke test covers every pending criterion
    # in this module at once. A decline or failed smoke test falls straight
    # through into the normal per-criterion loop below, unchanged.
    if mod["spec"].get("install_only") and mod["pending_criteria"]:
        install_command = mod["spec"].get("install_command")
        if not install_command:
            console.print(f"[red]{mod['name']} declares install_only but has no install_command — falling through to full build.[/red]")
        else:
            candidate_desc = (mod["spec"].get("build_vs_buy") or {}).get("rationale") or install_command
            ok, _findings = _run_install_only(
                pcp_dir, project_root, mod, criterion=None,
                install_command=install_command, candidate_desc=candidate_desc, yes=yes,
                budget=budget,
            )
            if ok:
                for c in mod["pending_criteria"]:
                    _mark_criterion_complete(mod, c["id"], verified_by="pcp_build_install_only")
                console.print(f"\n[green]✓ Module '{mod['name']}' built successfully (install-only)![/green]")
                return {"module": mod["name"], "success": True}

    if not _criteria_parallel_enabled(mod):
        for c in mod["pending_criteria"]:
            console.print(f"\n[bold underline]Criterion [{c['id']}]:[/bold underline] {c['description']}")
            success, block_findings = _build_one_criterion(pcp_dir, project_root, mod, c, build_model, build_model_explicit, budget, yes)

            if success:
                console.print(f"[green]✓ Criterion [{c['id']}] passed all gates successfully![/green]")
                _mark_criterion_complete(mod, c["id"])
                _write_progress(pcp_dir, mod["name"], c["id"], 0, "done")
            else:
                console.print(f"[red]✗ Failed to build Criterion [{c['id']}] after 3 attempts.[/red]")
                _record_escalation(pcp_dir, mod["name"], c["id"], block_findings)
                _write_progress(pcp_dir, mod["name"], c["id"], 0, "failed")
                return {"module": mod["name"], "success": False, "failed_criterion": c["id"], "block_findings": block_findings}

        console.print(f"\n[green]✓ Module '{mod['name']}' built successfully![/green]")
        return {"module": mod["name"], "success": True}

    # Opt-in path: criteria grouped into dependency waves, independent
    # criteria within a wave built concurrently, each in its own git
    # worktree nested off `project_root` (same _setup_worktree/_merge_
    # module_branch/_cleanup_worktree helpers as module-level parallelism,
    # just given a criterion-scoped unit name instead of a module name).
    wave_of = _compute_criterion_waves(mod)
    num_waves = max(wave_of.values(), default=0) + 1
    scheduled: list[list[dict]] = []
    for wave_number in range(num_waves):
        in_wave = [c for c in mod["pending_criteria"] if wave_of.get(c["id"], 0) == wave_number]
        if in_wave:
            scheduled.extend(_partition_wave_by_file_scope(in_wave))
    for wave_number, wave_criteria in enumerate(scheduled):
        if not wave_criteria:
            continue

        if len(wave_criteria) == 1:
            c = wave_criteria[0]
            console.print(f"\n[bold underline]Criterion [{c['id']}]:[/bold underline] {c['description']}")
            success, block_findings = _build_one_criterion(pcp_dir, project_root, mod, c, build_model, build_model_explicit, budget, yes)
            if success:
                console.print(f"[green]✓ Criterion [{c['id']}] passed all gates successfully![/green]")
                _mark_criterion_complete(mod, c["id"])
                _write_progress(pcp_dir, mod["name"], c["id"], 0, "done")
            else:
                console.print(f"[red]✗ Failed to build Criterion [{c['id']}] after 3 attempts.[/red]")
                _record_escalation(pcp_dir, mod["name"], c["id"], block_findings)
                _write_progress(pcp_dir, mod["name"], c["id"], 0, "failed")
                return {"module": mod["name"], "success": False, "failed_criterion": c["id"], "block_findings": block_findings}
            continue

        console.print(
            f"\n[bold]Criterion wave {wave_number}:[/bold] {len(wave_criteria)} independent "
            f"criteria in '{mod['name']}' building in parallel "
            f"(up to {min(_max_parallel_criteria(), len(wave_criteria))} at once, each in its own worktree)..."
        )
        units = {c["id"]: f"{mod['name']}-{c['id']}" for c in wave_criteria}
        worktrees = {c["id"]: _setup_worktree(project_root, units[c["id"]]) for c in wave_criteria}
        results: dict[str, tuple[bool, list[str]]] = {}
        with ThreadPoolExecutor(
            max_workers=min(_max_parallel_criteria(), len(wave_criteria))
        ) as executor:
            futures = {
                executor.submit(
                    _build_one_criterion, pcp_dir, worktrees[c["id"]], mod, c, build_model, build_model_explicit, budget, yes,
                ): c["id"]
                for c in wave_criteria
            }
            for future in as_completed(futures):
                cid = futures[future]
                try:
                    results[cid] = future.result()
                except BudgetExceeded:
                    # Mirrors the module-level parallel path's handling
                    # (build()'s own ThreadPoolExecutor loop below): convert
                    # to a graceful failure result rather than letting the
                    # run-level circuit breaker crash out with a raw
                    # traceback mid-wave.
                    results[cid] = (False, [f"budget circuit breaker: exceeded {budget.max_sessions} agent sessions this run"])

        any_failed = False
        failed_id = None
        failed_findings: list[str] = []
        for c in wave_criteria:
            cid = c["id"]
            success, block_findings = results.get(cid, (False, ["no result — worker crashed"]))
            if success:
                ok, merge_output = _merge_module_branch(project_root, units[cid], pcp_dir=pcp_dir)
                if ok:
                    _cleanup_worktree(project_root, units[cid], worktrees[cid])
                    console.print(f"[green]✓ Criterion [{cid}] passed all gates successfully![/green]")
                    _mark_criterion_complete(mod, cid)
                    _write_progress(pcp_dir, mod["name"], cid, 0, "done")
                else:
                    # A merge conflict here means two criteria in this wave
                    # genuinely touched the same code -- the collision that
                    # optimistic scheduling accepts as recoverable rather than
                    # prevents by serialising everything (see
                    # _partition_wave_by_file_scope). The merge already aborted
                    # cleanly, so the correct move is to rebuild this criterion
                    # against the now-updated main, not to stop the module and
                    # hand a human a git conflict.
                    #
                    # Bounded to one retry: a second conflict on the same
                    # criterion is not contention, it is something structural
                    # that a human should look at.
                    console.print(
                        f"[yellow]Criterion '{cid}' collided on merge with work that landed "
                        f"first in this wave — rebuilding it against the updated base.[/yellow]"
                    )
                    _cleanup_worktree(project_root, units[cid], worktrees[cid])
                    retry_wt = _setup_worktree(project_root, units[cid])
                    retry_ok, retry_findings = _build_one_criterion(
                        pcp_dir, retry_wt, mod, c, build_model, build_model_explicit, budget, yes,
                    )
                    remerged = False
                    if retry_ok:
                        remerged, merge_output = _merge_module_branch(
                            project_root, units[cid], pcp_dir=pcp_dir)
                    if remerged:
                        _cleanup_worktree(project_root, units[cid], retry_wt)
                        console.print(f"[green]✓ Criterion [{cid}] passed all gates after collision rebuild![/green]")
                        _mark_criterion_complete(mod, cid)
                        _write_progress(pcp_dir, mod["name"], cid, 0, "done")
                    else:
                        any_failed, failed_id = True, cid
                        failed_findings = retry_findings or [f"merge conflict after rebuild: {merge_output[-500:]}"]
                        console.print(
                            f"[red]✗ Criterion '{cid}' still could not be merged into "
                            f"'{mod['name']}' after a rebuild:[/red]\n{merge_output}"
                        )
                        console.print(f"[dim]Worktree left at {retry_wt} for manual resolution.[/dim]")
            else:
                any_failed, failed_id, failed_findings = True, cid, block_findings
                console.print(f"[red]✗ Failed to build Criterion [{cid}] after 3 attempts.[/red]")
                _record_escalation(pcp_dir, mod["name"], cid, block_findings)

        if any_failed:
            return {"module": mod["name"], "success": False, "failed_criterion": failed_id, "block_findings": failed_findings}

    console.print(f"\n[green]✓ Module '{mod['name']}' built successfully![/green]")
    return {"module": mod["name"], "success": True}


def _refresh_state(pcp_dir: Path, modules_dir: Path) -> None:
    """Regenerate current_state.md + pcp.md once — after a wave, not per
    criterion. Aggregating across all modules' acceptance.yaml is not safe
    to do per-criterion under parallel module builds (nothing meaningful
    to merge, and it's wasted work when only the -- soon to be reharvested
    -- last snapshot matters)."""
    from datetime import datetime, timezone
    from pcp.commands.scan import _scan_module, _write_current_state, _load_prior_manual_status
    prior_manual = _load_prior_manual_status(pcp_dir / "current_state.md")
    modules_results = []
    for af in sorted(modules_dir.glob("*/acceptance.yaml")):
        m_name = af.parent.name
        res = _scan_module(m_name, af, pcp_dir.parent, prior_manual)
        modules_results.append(res)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_current_state(pcp_dir, modules_results, timestamp)
    total = sum(len(m["criteria"]) for m in modules_results)
    complete = sum(1 for m in modules_results for c in m["criteria"] if c["status"] == "complete")
    write_pcp_md(pcp_dir, modules_results, timestamp, total, complete)


@click.command()
@click.option("--module", "module_name", default=None,
              help="Build specific module only.")
@click.option("--path", "project_path", type=click.Path(), default=None,
              help="Project root override.")
@click.option("--yes", "yes", is_flag=True,
              help="Skip the interactive install-only approval prompt (CI/non-interactive use — opt-in, not default). Only affects criteria/modules declaring install_only; every other criterion builds exactly as before.")
def build(module_name: str | None, project_path: str | None, yes: bool):
    """Run autonomous AI coding loops for pending acceptance criteria."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    from datetime import datetime, timezone
    _build_start_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    from pcp.commands.doctor import check_environment
    check_environment(pcp_dir)

    # Self-capture the CALLING session before the objective-conflict gate below
    # even looks at brd_items.yaml. Real incident, 2026-07-22: a correction was
    # discussed and "go ahead" given in the SAME still-open Claude Code session
    # -- `pcp capture` is normally wired to a SessionEnd hook, which never fires
    # until the session ends, so the correction never got classified at all and
    # the gate below would have found zero conflicts to block on. This makes
    # `pcp build` capture its own live, still-open session -- deterministic,
    # not dependent on a skill/orchestrator remembering to call `pcp capture`
    # itself. Advisory, never blocks: any failure here must not stop a build
    # that has nothing to do with capture working.
    _self_session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if _self_session_id:
        try:
            _self_transcript = find_transcript_for_session(_self_session_id)
            if _self_transcript:
                console.print("[dim]Capturing current session for business/technical drift before build...[/dim]")
                run_capture(pcp_dir, _self_transcript, source=f"session:{_self_session_id}", session_id=_self_session_id)
        except Exception as e:
            console.print(f"[dim]Self-capture skipped: {e}[/dim]")

    # Objective-conflict gate (CTRL-035) -- a captured business decision that
    # conflicts with objective.md/target_state.md's actual text must not sit
    # silently in brd.md prose while a build cycle spends millions of tokens
    # against the stale target. reconcile() also auto-clears any conflict
    # whose flagged objective_hash no longer matches current file content --
    # i.e. a human already made the edit -- so this only blocks on conflicts
    # nobody has actually resolved yet. See objective_conflicts.py.
    unresolved_conflicts = objective_conflicts.reconcile(pcp_dir)
    if unresolved_conflicts:
        telemetry.record(
            pcp_dir, cycle="build", check="objective-conflict-gate", control_id="CTRL-035",
            result="blocked", error_count=len(unresolved_conflicts),
            errors=[c.get("id", "?") for c in unresolved_conflicts],
        )
        console.print("[bold red]Build blocked -- unresolved objective conflict(s):[/bold red]")
        for c in unresolved_conflicts:
            console.print(f"  [red]{c.get('id')}[/red]: {c.get('description')}")
            console.print(f"    [dim]conflict: {c.get('drift_flag')}[/dim]")
        # This message used to say "rewrite the spec by hand (spec files stay
        # human-only)" -- the exact doctrine bug corrected on 2026-07-25:
        # protected files are human-AUTHORIZED, not human-TYPED, and every one
        # of them has a propose -> real-diff -> approve -> write path. Sending
        # the user off to hand-edit, without naming the command built for
        # precisely this moment (`--from-conflict` exists to pull the
        # correction text straight out of the flagged item), was PCP telling
        # people to bypass its own gated mechanism.
        first_id = unresolved_conflicts[0].get("id", "<ID>")
        console.print(
            "\n[yellow]A captured business decision conflicts with objective.md/target_state.md.[/yellow]"
        )
        console.print(
            f"  Resolve it:      [dim]pcp correct-objective --from-conflict {first_id}[/dim]\n"
            f"                   [dim](LLM proposes the rewrite, you approve a real diff before anything is written)[/dim]\n"
            f"  False positive:  [dim]pcp objective-conflicts --dismiss {first_id} --reason \"...\"[/dim]"
        )
        sys.exit(2)
    telemetry.record(
        pcp_dir, cycle="build", check="objective-conflict-gate", control_id="CTRL-035", result="pass",
    )

    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        console.print("[yellow]No modules found. Run `pcp kickoff` or `pcp init` first.[/yellow]")
        sys.exit(0)

    project_root = pcp_dir.parent

    modules_to_build = gather_modules_to_build(pcp_dir, module_name)

    if not modules_to_build:
        console.print("[green]All acceptance criteria are complete. Nothing to build![/green]")
        sys.exit(0)

    # Self-reporting nudge, 2026-07-20 -- found dogfooding two real projects
    # where every build was called --module X one at a time, leaving genuine
    # wave-parallelism headroom (10+ independent modules in some waves)
    # completely unused. --module is often the right call (reviewing one
    # module's PR before starting the next), but a human/orchestrator should
    # at least see what they're trading away, not discover it by re-reading
    # the wave-parallelism docs later.
    if module_name:
        other_pending = gather_modules_to_build(pcp_dir, None)
        other_count = len([m for m in other_pending if m["name"] != module_name])
        if other_count:
            console.print(
                f"[yellow]Note:[/yellow] {other_count} other module(s) also have pending criteria. "
                "`--module` builds this one alone -- run `pcp build` with no `--module` filter to let "
                "the wave engine build independent modules concurrently instead."
            )

    # Order modules into dependency waves. Modules within a wave have no
    # declared dependency on each other by construction — the wave boundary
    # is the real gate, not build order within it — so they build in
    # parallel, each in its own git worktree + branch (mirrors the /pcp
    # skill's Branch Isolation Protocol). Criteria within one module stay
    # sequential (each builds on the prior commit).
    wave_of = _compute_waves(modules_to_build)
    modules_to_build.sort(key=lambda m: wave_of.get(m["name"], 0))
    num_waves = max(wave_of.values(), default=0) + 1
    if num_waves > 1:
        order_desc = ", ".join(f"{m['name']}(w{wave_of[m['name']]})" for m in modules_to_build)
        console.print(f"[dim]Build order: {num_waves} wave(s) by dependency — {order_desc}[/dim]")

    # Marks this process (and any subprocess it spawns — the coding agent, and
    # any git commit that agent runs via its own shell access) as an automated
    # build-agent session. check.py's protected_path rule (R003) only hard-blocks
    # spec-file edits when this is set — a human's own interactive commit never
    # sets it and is never blocked from editing spec files directly.
    os.environ["PCP_AGENT_SESSION"] = "1"
    check_agent_depth_or_exit()

    timeout_sec, timeout_is_default = qa.test_timeout_info()
    if timeout_is_default:
        console.print(
            f"[yellow]Note:[/yellow] PCP_QA_TEST_TIMEOUT_SEC not set -- test-suite gate uses the "
            f"{timeout_sec}s default. A slow-but-passing suite and a hung dependency (wrong/unreachable "
            "DB, a squatted port) both surface identically as \"timed out\" -- set it explicitly if this "
            "project's real suite legitimately runs long."
        )
    else:
        console.print(f"[dim]QA test-suite timeout: {timeout_sec}s (PCP_QA_TEST_TIMEOUT_SEC).[/dim]")

    budget = _BuildBudget(_max_build_sessions())
    # Model-selection strategy (see llm/client.py) -- Sonnet is the reviewed
    # default for the coding agent, escalating to Opus on a criterion's
    # final attempt. A human's explicit PCP_BUILD_MODEL always wins outright
    # and disables escalation -- an explicit override on attempt 1 shouldn't
    # silently change model again on attempt 3 without being asked.
    _explicit_build_model = os.environ.get("PCP_BUILD_MODEL")
    build_model = _explicit_build_model or llm.BUILD_MODEL
    build_model_explicit = bool(_explicit_build_model)
    max_parallel = _max_parallel_modules()

    for wave_number in range(num_waves):
        wave_modules = [m for m in modules_to_build if wave_of.get(m["name"], 0) == wave_number]
        if not wave_modules:
            continue

        wave_start_ref = _git_head(project_root)
        use_worktrees = len(wave_modules) > 1 and max_parallel > 1

        if not use_worktrees:
            # Single module (or parallelism disabled) — run directly against
            # the main project root, no worktree machinery needed at all.
            for mod in wave_modules:
                result = _build_module_worker(pcp_dir, mod, project_root, build_model, build_model_explicit, budget, yes)
                if not result["success"]:
                    console.print("[bold red]Build execution stopped. Please resolve findings manually.[/bold red]")
                    sys.exit(1)
        else:
            console.print(
                f"\n[bold]Wave {wave_number}:[/bold] building {len(wave_modules)} module(s) in parallel "
                f"(up to {min(max_parallel, len(wave_modules))} at once, each in its own worktree)..."
            )
            worktrees = {mod["name"]: _setup_worktree(project_root, mod["name"]) for mod in wave_modules}
            results = {}
            try:
                with ThreadPoolExecutor(max_workers=min(max_parallel, len(wave_modules))) as executor:
                    futures = {
                        executor.submit(
                            _build_module_worker, pcp_dir, mod, worktrees[mod["name"]], build_model, build_model_explicit, budget, yes,
                        ): mod["name"]
                        for mod in wave_modules
                    }
                    for future in as_completed(futures):
                        m_name = futures[future]
                        try:
                            results[m_name] = future.result()
                        except BudgetExceeded:
                            results[m_name] = {"module": m_name, "success": False, "budget_exceeded": True}
            finally:
                pass

            # Merge successful modules' branches back into main, serialized
            # (git operations on the same repo should not run concurrently).
            # Failed modules' worktrees are left in place for inspection —
            # never silently discarded.
            any_failed = False
            for mod in wave_modules:
                m_name = mod["name"]
                result = results.get(m_name, {"success": False})
                if result["success"]:
                    ok, merge_output = _merge_module_branch(project_root, m_name, pcp_dir=pcp_dir)
                    if ok:
                        _cleanup_worktree(project_root, m_name, worktrees[m_name])
                    else:
                        any_failed = True
                        console.print(f"[red]✗ Merge conflict bringing '{m_name}' back into main:[/red]\n{merge_output}")
                        console.print(f"[dim]Worktree left at {worktrees[m_name]} for manual resolution.[/dim]")
                else:
                    any_failed = True
                    if result.get("budget_exceeded"):
                        console.print(f"[red]'{m_name}' stopped — run-level session budget exceeded.[/red]")
                    console.print(f"[dim]Worktree left at {worktrees[m_name]} for inspection.[/dim]")

            if any_failed:
                console.print("[bold red]Build execution stopped. Please resolve findings manually.[/bold red]")
                sys.exit(1)

        # Advisory dead-code/bloat sweep + audit-evidence refresh — once per
        # wave, after all this wave's modules are merged into main.
        try:
            from pcp.commands.audit import _run_audit, _write_audit_md
            from datetime import datetime, timezone
            audit_result = _run_audit(project_root)
            audit_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _write_audit_md(pcp_dir, audit_result, audit_ts)
            if audit_result["tool"]:
                console.print(
                    f"[dim]Audit: {len(audit_result['findings'])} dead-code finding(s) "
                    f"({audit_result['tool']}) → .pcp/audit.md[/dim]"
                )
        except Exception as e:
            console.print(f"[dim]Audit skipped: {e}[/dim]")

        try:
            from pcp.commands.provenance import write_provenance
            write_provenance(pcp_dir)
        except Exception as e:
            console.print(f"[dim]Provenance refresh skipped: {e}[/dim]")

        try:
            from pcp.commands.docs import write_module_docs
            for mod in wave_modules:
                write_module_docs(pcp_dir, mod["spec_path"].parent)
        except Exception as e:
            console.print(f"[dim]Module docs refresh skipped: {e}[/dim]")

        try:
            from pcp.commands.design_audit import write_design_audit
            write_design_audit(pcp_dir)
        except Exception as e:
            console.print(f"[dim]Design audit refresh skipped: {e}[/dim]")

        _refresh_state(pcp_dir, modules_dir)

        if num_waves > 1:
            console.print(f"\n[bold]Wave {wave_number} merge checks...[/bold]")
        wave_findings = _run_wave_merge(pcp_dir, wave_modules, wave_start_ref, wave_number)
        if wave_findings:
            console.print("[red bold]BLOCKED — wave merge findings:[/red bold]")
            for f in wave_findings:
                console.print(f"  ✗ {f}")
            # A wave BLOCK used to print this and exit, leaving every criterion
            # in the wave marked `complete`. On 2026-07-27 the wave-level
            # architect review found a path-traversal vulnerability -- arbitrary
            # local file read through the one method named to keep that path
            # narrow -- said "fix before the next wave proceeds", and the
            # criteria that introduced it stayed complete. `pcp scan`,
            # current_state.md and the dashboard all reported them done, and
            # nothing recorded that the finding was ever raised.
            #
            # A gate that stops forward progress but leaves the defective work
            # marked verified is advisory in practice. If the wave says the work
            # is wrong, the work is not done: reopen it so the next run rebuilds
            # it WITH the finding as feedback, and record an escalation so the
            # finding survives the process that printed it.
            _reopen_wave_criteria(pcp_dir, wave_modules, wave_number, wave_findings)
            console.print("[bold red]Fix these before the next wave proceeds.[/bold red]")
            sys.exit(1)
        elif num_waves > 1:
            console.print(f"[green]✓ Wave {wave_number} merge checks passed.[/green]")

        # Step 3 of the global Build Cycle — push once this wave's merges are
        # clean, not just at the very end, so completed work is safe on the
        # remote even if a later wave fails.
        _auto_push(project_root)

    # Build Cycle Report (2026-07-24) — the evidence pcp build already
    # generates (run_log proof-of-delivery, .pcp/evidence/, telemetry.jsonl)
    # gets handed to the human here instead of sitting in files nobody
    # opens. Never fails the run — a report-writing bug must not fail a
    # build that otherwise succeeded.
    try:
        from pcp import build_report
        report_path = build_report.write(pcp_dir, _build_start_ts)
        console.print(f"\n[bold]Build Cycle Report[/bold] → {report_path.relative_to(project_root)}")
    except Exception as e:
        console.print(f"[dim]Build report skipped: {e}[/dim]")

    console.print(
        f"\n[bold]Run total:[/bold] {budget.session_count} agent session(s), "
        f"~${budget.run_cost_total:.2f} (build-agent only — see .pcp/token_ledger.yaml for judge-call spend too)"
    )

    # Auto-summarize telemetry — baked into the lifecycle, not a separate manual step.
    try:
        records = telemetry.load(pcp_dir)
        agg = telemetry.aggregate(records)
        total_qa = sum(v["qa_total"] for v in agg["by_module"].values())
        total_blocks = sum(v["qa_blocks"] for v in agg["by_module"].values())
        total_attempts = len(agg["build_records"])
        total_criteria = len({(m, c) for m, v in agg["by_module"].items() for c in v["criteria"]})
        avg_attempts = total_attempts / total_criteria if total_criteria else 0.0
        qa_rate = f"{total_blocks}/{total_qa}" if total_qa else "—"
        console.print(
            f"[dim]Telemetry: {total_criteria} criteria, {avg_attempts:.1f} avg attempts/criterion, "
            f"QA blocks {qa_rate} → .pcp/telemetry.jsonl ([cyan]pcp telemetry[/cyan] for full breakdown)[/dim]"
        )
    except Exception as e:
        console.print(f"[dim]Telemetry summary skipped: {e}[/dim]")

    # Step 4+ of the global Build Cycle — the cycle isn't done at "committed
    # and pushed," it's done when the product is actually running. `pcp
    # deploy` already owns the checklist/gate/mandatory-approval/smoke-test
    # machinery (irreversible production action — human approval stays
    # mandatory, `--yes` still opts out for CI, unaffected by this build
    # run's own --yes which only ever covered the install-only prompt). This
    # just stops deploy from being a separate step a human has to remember
    # to run — every successful build run surfaces the approval prompt
    # itself. No-op (never blocks a successful build) if no deploy command
    # is configured yet, or if this session has no attached terminal (a
    # blocking confirm() prompt would just hang a headless/CI run — those
    # cases still need an explicit `pcp deploy` call).
    from pcp.commands.doctor import load_integrations
    from pcp.commands.deploy import deploy as deploy_cmd
    deploy_command = (load_integrations(pcp_dir).get("deploy") or {}).get("command")
    if deploy_command and sys.stdin.isatty():
        console.print("\n[bold]Build cycle complete — running deploy checklist...[/bold]")
        try:
            deploy_cmd.callback(project_path=str(project_root), yes=False, rollout="100")
        except SystemExit as e:
            if e.code:
                console.print(f"[dim]Deploy step exited ({e.code}) — the build itself still succeeded.[/dim]")
    elif deploy_command:
        console.print("[dim]Deploy configured but this session has no attached terminal — run `pcp deploy` manually to ship this.[/dim]")
    else:
        console.print("[dim]No deploy command configured — run `pcp doctor` then `pcp deploy` to ship this.[/dim]")
