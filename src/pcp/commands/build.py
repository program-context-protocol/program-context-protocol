"""pcp build — autonomous agent execution loop to implement pending criteria."""

import json
import os
import re
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
from pcp.schema.validator import validate_file, load_yaml
from pcp.llm import client as llm
from pcp.llm.client import _claude_bin, _log_usage
from pcp.pcp_status import write_pcp_md
from pcp import decision_log
from pcp import telemetry
from pcp import qa
from pcp import evidence
from pcp import spend
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
    wave boundary is the only real gate). Kept conservative for unattended
    CLI use, where a human isn't necessarily watching cost accrue in real
    time the way an interactive orchestrator session is. Override with
    PCP_BUILD_MAX_PARALLEL."""
    return int(os.environ.get("PCP_BUILD_MAX_PARALLEL", "3"))


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

    def take_session(self) -> None:
        with self._lock:
            self.session_count += 1
            if self.session_count > self.max_sessions:
                self.tripped = True
                raise BudgetExceeded(self.session_count)

    def add_cost(self, cost: float | None) -> None:
        with self._lock:
            self.run_cost_total += cost or 0


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

def _criteria_parallel_enabled(mod: dict) -> bool:
    """A module opts in by having ANY criterion declare `depends_on` (even
    an empty list — presence, not content, is the signal, same convention
    as design_justification/build_vs_buy elsewhere in this schema: a
    deliberately-declared field, not an inferred one)."""
    return any(c.get("depends_on") is not None for c in mod["pending_criteria"])


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


def _setup_worktree(project_root: Path, module_name: str) -> Path:
    wt_path = _worktree_dir(project_root, module_name)
    if wt_path.exists():
        return wt_path  # reuse from a prior interrupted run
    branch = f"feat/{module_name}"
    branch_exists = subprocess.run(
        ["git", "rev-parse", "--verify", branch], cwd=project_root, capture_output=True,
    ).returncode == 0
    cmd = ["git", "worktree", "add", str(wt_path)] + ([branch] if branch_exists else ["-b", branch])
    subprocess.run(cmd, cwd=project_root, capture_output=True, text=True)
    return wt_path


def _merge_module_branch(project_root: Path, module_name: str, pcp_dir: Path | None = None) -> tuple[bool, str]:
    branch = f"feat/{module_name}"
    result = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}"],
        cwd=project_root, capture_output=True, text=True,
    )
    ok = result.returncode == 0
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


def _wave_record(pcp_dir: Path, wave_number: int, check: str, control_id: str, errors: list[str],
                  files: list[str] | None = None, result: str | None = None,
                  evidence_path: str | None = None) -> None:
    """Wave-merge gates have no single criterion_id/attempt — record at cycle_number=wave_number
    so they still land in the same telemetry.jsonl audit trail as per-criterion QA checks,
    instead of only ever reaching the user as a console line."""
    if result is None:
        result = "block" if errors else "pass"
    telemetry.record(
        pcp_dir,
        cycle="qa", cycle_number=wave_number, check=f"wave-{check}", control_id=control_id,
        module=None, submodule=None, criterion_id=None,
        files=files or [], result=result, errors=errors, error_count=len(errors),
        evidence_path=evidence_path,
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
            scorers_disagree = (
                vs.get("scoring_method") == "deterministic"
                and llm_score is not None
                and llm_score >= 0.85
                and vs.get("coverage_score", 0) < llm_score
            )
            if coverage_gaps and scorers_disagree and not severe_coupling:
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
)
_PCP_OPERATIONAL_DIRS = (".pcp/evidence/", ".pcp/transcripts/")


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
    return result.stdout[:14000]


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
        prompt_parts.append(
            "This criterion renders user-facing UI. Read `.pcp/design_system.md` first "
            "and apply its established tokens/conventions rather than deciding a look "
            "fresh — if it's still the empty scaffold, this is the first UI screen: "
            "establish the system now (see the `pcp-ui-design` skill) and write it there "
            "so later screens stay consistent instead of each looking like a different "
            "vanilla template. Before finishing, add a `design_justification` block to "
            "this criterion in acceptance.yaml: `checklist_passed` (which design-system "
            "conventions this screen actually followed), `jtbd_framing` (one sentence, "
            "'when a user is X, this lets them Y' — not a restatement of the description), "
            "and `deviations_from_system` if this screen needed a new pattern the system "
            "didn't have yet. If a `webapp-testing` skill is available, use it to actually "
            "load the running page and verify it renders/behaves as intended before "
            "finishing — don't just trust that the code compiles."
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
        for r in protected_rules:
            if r.get("severity") == "hard_block":
                v = run_protected_path_rule(r, changed_files)
                if v:
                    msg = f"Protected Path Rule [{r['id']}] {r['name']} violation: {', '.join(v)}"
                    if r.get("message"):
                        msg += f" → Fix: {r['message']}"
                    violations.append(msg)
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
    except Exception:
        violations.append("Invalid ci_rules.yaml schema")

    _qa_record(pcp_dir, ctx, "layer1", violations, control_id="CTRL-004", files=changed_files, tool="ci_rules.yaml")
    return violations


def _run_test_suite_check(pcp_dir: Path, project_root: Path, ctx: dict) -> list[str]:
    """Full regression suite — project-wide. Skips (never blocks) if no test runner detected."""
    result = qa.run_test_suite(project_root)
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


def _run_lint_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> list[str]:
    """Lint on changed files only. Skips (never blocks) if no linter detected."""
    result = qa.run_lint(project_root, changed_files)
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


def _run_sast_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> list[str]:
    """SAST + secret-scan via semgrep, if installed. Scoped to changed files."""
    result = qa.run_sast(project_root, changed_files)
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
        console.print(f"[yellow]Warning: Architect review call failed: {e}[/yellow]")
        _qa_record(
            pcp_dir, ctx, "architect-review", [f"call failed: {e}"],
            control_id="CTRL-005", files=changed_files, result="error",
        )
        return []

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
        console.print(f"[yellow]Warning: Gate check call failed: {e}[/yellow]")
        _qa_record(pcp_dir, ctx, "gate", [f"call failed: {e}"], control_id="CTRL-006", result="error")
        return []

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


def _run_design_consistency_check(pcp_dir: Path, project_root: Path, criterion: dict, ctx: dict) -> None:
    """PCP Design lifecycle, stage 4 (Verify). Advisory only — never returned
    into block_findings, never blocks a criterion. Only fires for UI-facing
    criteria once .pcp/design_system.md has real established color tokens
    (not the empty scaffold): flags hardcoded hex color literals in the
    criterion's target file as a heuristic signal the screen may not be
    using the project's own design system. Not proof either way — a
    legitimate reason to hardcode a specific value (a brand-mandated exact
    color) is common; this surfaces a signal for human review, same posture
    as pcp audit's dead-code findings."""
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
    hex_matches = re.findall(r"#[0-9a-fA-F]{3,8}\b", content)
    findings = []
    if hex_matches:
        findings.append(
            f"{len(hex_matches)} hardcoded hex color literal(s) in {target} while "
            f".pcp/design_system.md has established color tokens — consider reusing "
            f"those instead. Examples: {', '.join(hex_matches[:5])}"
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
            f"{target} references none of the {len(declared_tokens)} named design-system "
            "tokens (--*) declared in .pcp/design_system.md — the screen may be styled "
            "outside the system entirely"
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
    _qa_record(
        pcp_dir, ctx, "build-scope", findings, control_id="CTRL-018", tool="git-diff",
        evidence_path=evidence_path,
    )
    if findings and mode == "warn":
        console.print(f"[yellow]Scope guard (advisory):[/yellow] {findings[0]}")
        return []
    return findings


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


def _build_one_criterion(
    pcp_dir: Path, project_root: Path, mod: dict, c: dict,
    build_model: str | None, build_model_explicit: bool, budget: "_BuildBudget",
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
    feedback = None
    success = False
    block_findings: list[str] = []
    attempt_history: list[str] = []
    agent_session_id = str(uuid.uuid4())
    # Everything the agent does this criterion — committed or not — is
    # measured against this ref, so committing can't hide work from gates.
    criterion_start_ref = _git_head(project_root)

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

    for attempt in range(1, 4):
        console.print(f"\n[dim]Attempt {attempt}/3 — {mod['name']}/{c['id']}...[/dim]")

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

        # Running gates — QA (test suite, lint, SAST) first, then architecture/alignment.
        # Deliberately outside _STATE_LOCK: these are independent per module
        # (LLM calls + subprocess test/lint/SAST runs), exactly the work that
        # should overlap across concurrently-building modules. Each check
        # function's own _qa_record call is internally lock-guarded.
        console.print(f"[dim]Evaluating gates ({mod['name']}/{c['id']})...[/dim]")
        ctx = {
            "module": mod["name"], "submodule": None, "criterion_id": c["id"],
            "criterion_description": c.get("description", ""),
            "attempt": attempt, "files": changed_files,
        }
        violations_tests = _run_test_suite_check(pcp_dir, project_root, ctx)
        violations_lint = _run_lint_check(pcp_dir, project_root, changed_files, ctx)
        violations_sast = _run_sast_check(pcp_dir, project_root, changed_files, ctx)
        violations_l1 = _run_layer1_check(pcp_dir, project_root, changed_files, ctx)
        violations_scope = _run_scope_check(pcp_dir, mod, c, changed_files, ctx)
        violations_arch = _run_architect_review(pcp_dir, diff, changed_files, ctx)
        violations_gate = _run_gate_check(pcp_dir, diff, ctx)
        _run_design_consistency_check(pcp_dir, project_root, c, ctx)
        violations_design_justification = _run_design_justification_check(pcp_dir, mod, c, ctx)
        violations_bvb_justification = _run_build_vs_buy_justification_check(pcp_dir, mod, c, ctx)

        block_findings = (
            violations_tests + violations_lint + violations_sast
            + violations_l1 + violations_scope + violations_arch + violations_gate
            + violations_design_justification + violations_bvb_justification
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

    return success, block_findings


def _mark_criterion_complete(mod: dict, criterion_id: str) -> None:
    """No cross-module contention (each module only ever touches its own
    acceptance.yaml), but still guarded for consistency — and, under
    criterion-level parallelism, this same file IS written by concurrent
    threads for different criteria in the same module, so the guard is load-
    bearing there, not just defensive."""
    with _STATE_LOCK:
        acc_data = load_yaml(mod["acc_path"])
        for crit in acc_data.get("criteria", []):
            if crit["id"] == criterion_id:
                crit["status"] = "complete"
        mod["acc_path"].write_text(yaml.dump(acc_data, default_flow_style=False))


def _build_module_worker(
    pcp_dir: Path, mod: dict, project_root: Path,
    build_model: str | None, build_model_explicit: bool, budget: "_BuildBudget",
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

    if not _criteria_parallel_enabled(mod):
        for c in mod["pending_criteria"]:
            console.print(f"\n[bold underline]Criterion [{c['id']}]:[/bold underline] {c['description']}")
            success, block_findings = _build_one_criterion(pcp_dir, project_root, mod, c, build_model, build_model_explicit, budget)

            if success:
                console.print(f"[green]✓ Criterion [{c['id']}] passed all gates successfully![/green]")
                _mark_criterion_complete(mod, c["id"])
            else:
                console.print(f"[red]✗ Failed to build Criterion [{c['id']}] after 3 attempts.[/red]")
                _record_escalation(pcp_dir, mod["name"], c["id"], block_findings)
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
    for wave_number in range(num_waves):
        wave_criteria = [c for c in mod["pending_criteria"] if wave_of.get(c["id"], 0) == wave_number]
        if not wave_criteria:
            continue

        if len(wave_criteria) == 1:
            c = wave_criteria[0]
            console.print(f"\n[bold underline]Criterion [{c['id']}]:[/bold underline] {c['description']}")
            success, block_findings = _build_one_criterion(pcp_dir, project_root, mod, c, build_model, build_model_explicit, budget)
            if success:
                console.print(f"[green]✓ Criterion [{c['id']}] passed all gates successfully![/green]")
                _mark_criterion_complete(mod, c["id"])
            else:
                console.print(f"[red]✗ Failed to build Criterion [{c['id']}] after 3 attempts.[/red]")
                _record_escalation(pcp_dir, mod["name"], c["id"], block_findings)
                return {"module": mod["name"], "success": False, "failed_criterion": c["id"], "block_findings": block_findings}
            continue

        console.print(
            f"\n[bold]Criterion wave {wave_number}:[/bold] {len(wave_criteria)} independent "
            f"criteria in '{mod['name']}' building in parallel (each in its own worktree)..."
        )
        units = {c["id"]: f"{mod['name']}-{c['id']}" for c in wave_criteria}
        worktrees = {c["id"]: _setup_worktree(project_root, units[c["id"]]) for c in wave_criteria}
        results: dict[str, tuple[bool, list[str]]] = {}
        with ThreadPoolExecutor(max_workers=len(wave_criteria)) as executor:
            futures = {
                executor.submit(
                    _build_one_criterion, pcp_dir, worktrees[c["id"]], mod, c, build_model, build_model_explicit, budget,
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
                else:
                    any_failed, failed_id = True, cid
                    console.print(f"[red]✗ Merge conflict bringing criterion '{cid}' back into '{mod['name']}':[/red]\n{merge_output}")
                    console.print(f"[dim]Worktree left at {worktrees[cid]} for manual resolution.[/dim]")
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
def build(module_name: str | None, project_path: str | None):
    """Run autonomous AI coding loops for pending acceptance criteria."""
    try:
        pcp_dir = find_pcp_dir(Path(project_path) if project_path else None)
    except NoPCPDir as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(2)

    from pcp.commands.doctor import check_environment
    check_environment(pcp_dir)

    modules_dir = get_modules_dir(pcp_dir)
    if not modules_dir.exists():
        console.print("[yellow]No modules found. Run `pcp kickoff` or `pcp init` first.[/yellow]")
        sys.exit(0)

    project_root = pcp_dir.parent

    modules_to_build = gather_modules_to_build(pcp_dir, module_name)

    if not modules_to_build:
        console.print("[green]All acceptance criteria are complete. Nothing to build![/green]")
        sys.exit(0)

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
                result = _build_module_worker(pcp_dir, mod, project_root, build_model, build_model_explicit, budget)
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
                            _build_module_worker, pcp_dir, mod, worktrees[mod["name"]], build_model, build_model_explicit, budget,
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

        _refresh_state(pcp_dir, modules_dir)

        if num_waves > 1:
            console.print(f"\n[bold]Wave {wave_number} merge checks...[/bold]")
        wave_findings = _run_wave_merge(pcp_dir, wave_modules, wave_start_ref, wave_number)
        if wave_findings:
            console.print("[red bold]BLOCKED — wave merge findings:[/red bold]")
            for f in wave_findings:
                console.print(f"  ✗ {f}")
            console.print("[bold red]Fix these before the next wave proceeds.[/bold red]")
            sys.exit(1)
        elif num_waves > 1:
            console.print(f"[green]✓ Wave {wave_number} merge checks passed.[/green]")

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
