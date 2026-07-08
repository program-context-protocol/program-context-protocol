"""pcp build — autonomous agent execution loop to implement pending criteria."""

import json
import os
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
from pcp import telemetry
from pcp import qa
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


def _merge_module_branch(project_root: Path, module_name: str) -> tuple[bool, str]:
    branch = f"feat/{module_name}"
    result = subprocess.run(
        ["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}"],
        cwd=project_root, capture_output=True, text=True,
    )
    return result.returncode == 0, (result.stdout + result.stderr)


def _cleanup_worktree(project_root: Path, module_name: str, wt_path: Path) -> None:
    subprocess.run(["git", "worktree", "remove", str(wt_path), "--force"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "branch", "-D", f"feat/{module_name}"], cwd=project_root, capture_output=True)


def _wave_record(pcp_dir: Path, wave_number: int, check: str, control_id: str, errors: list[str],
                  files: list[str] | None = None, result: str | None = None) -> None:
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
    if test_result["tool"] and not test_result["passed"]:
        test_findings.append(f"Wave integration suite ({test_result['tool']}) FAILED:\n{test_result['output'][-1500:]}")
    _wave_record(pcp_dir, wave_number, "test-suite", "CTRL-001", test_findings, files=wave_mod_names,
                 result="skipped" if not test_result["tool"] else None)
    findings += test_findings

    # 3. validate-strategy re-check — coverage/coupling after this wave's changes.
    try:
        from pcp.commands.validate_strategy import run_validate_strategy
        vs = run_validate_strategy(pcp_dir, command="wave-validate-strategy")
        strategy_findings: list[str] = []
        if vs:
            severe_coupling = [v for v in vs.get("coupling_violations", []) if v.get("type") in ("circular", "god_module", "shared_state")]
            if vs.get("coverage_gaps") or severe_coupling:
                strategy_findings.append(
                    f"validate-strategy: coverage={vs.get('coverage_score', 0):.0%}, "
                    f"coupling={vs.get('coupling_score', 1):.0%}, "
                    f"gaps={len(vs.get('coverage_gaps', []))}, "
                    f"severe coupling violations={len(severe_coupling)} (circular/god_module/shared_state)"
                )
        _wave_record(pcp_dir, wave_number, "validate-strategy", "CTRL-008", strategy_findings, files=wave_mod_names)
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
            res = llm.call_json(ARCH_SYSTEM_PROMPT, prompt, model=llm.JUDGE_MODEL, pcp_dir=pcp_dir, command="wave-architect-review")
            for f in res.get("findings", []):
                if f.get("severity") == "BLOCK":
                    arch_findings.append(f"Wave architect-review: {f.get('location', 'general')}: {f.get('finding', '')} → Fix: {f.get('fix', '')}")
            _wave_record(pcp_dir, wave_number, "architect-review", "CTRL-005", arch_findings, files=changed)
        findings += arch_findings
    except Exception as e:
        console.print(f"[yellow]Warning: wave architect-review failed: {e}[/yellow]")
        _wave_record(pcp_dir, wave_number, "architect-review", "CTRL-005", [f"call failed: {e}"],
                     files=wave_mod_names, result="error")

    return findings


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


def _get_working_diff(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        capture_output=True, text=True, cwd=cwd,
    )
    if result.returncode != 0:
        # Fallback to general diff
        result = subprocess.run(
            ["git", "diff"],
            capture_output=True, text=True, cwd=cwd,
        )
    return result.stdout[:14000]


def _build_agent_prompt(
    pcp_dir: Path,
    module_name: str,
    criterion: dict,
    spec: dict,
) -> str:
    """First-attempt prompt. You have filesystem access — read .pcp/ context yourself
    instead of having it pasted here. Pasting it costs input tokens on every single
    criterion/attempt for content that's identical across the whole build run."""
    prompt_parts = [
        "You are an AI coding agent implementing an acceptance criterion for a program module.",
        "Your task is to write/modify code in the project to implement this feature.",
        f"Module: {module_name}",
        f"Criterion: [{criterion['id']}] {criterion['description']}",
        "",
        "Before editing, read these files yourself for context (don't ask — just Read them):",
        "- .pcp/objective.md       (program objective)",
        "- .pcp/architecture.md    (tech decisions + constraints)",
        "- .pcp/architect_persona.md (architecture review principles — your code must satisfy these)",
        "- .pcp/current_state.md   (what's already built)",
        "",
        "## Module Specification",
        yaml.dump(spec, default_flow_style=False),
        "",
        "Follow TDD: write a failing test for this criterion first, confirm it fails, "
        "then write the implementation and confirm the test passes. The full test suite, "
        "lint, and a SAST/secret scan will run against your changes after you finish — "
        "fix anything those would flag before considering the criterion done.",
        "Use editing tools to modify files and run tests to verify your implementation.",
    ]
    return "\n".join(prompt_parts)


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
    tool: str | None = _NOTSET, result: str | None = None,
) -> None:
    """Records one gate outcome. `result` resolution order: explicit override,
    then "skipped" if a tool-based check found no tool installed, then
    block/pass from `errors`. A skip must never collapse into "pass" — that's
    what makes an unenforced control invisible in the audit trail."""
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
            result=result, errors=errors, error_count=len(errors),
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
    if result["tool"] and not result["passed"]:
        violations.append(
            f"Test suite ({result['tool']}) FAILED:\n{result['output'][-1500:]}"
        )
    _qa_record(pcp_dir, ctx, "test-suite", violations, control_id="CTRL-001", tool=result["tool"])
    return violations


def _run_lint_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> list[str]:
    """Lint on changed files only. Skips (never blocks) if no linter detected."""
    result = qa.run_lint(project_root, changed_files)
    violations: list[str] = []
    if result["tool"] and not result["passed"]:
        issues = "\n".join(result["issues"][:10])
        violations.append(f"Lint ({result['tool']}) found issues:\n{issues}")
    _qa_record(pcp_dir, ctx, "lint", violations, control_id="CTRL-002", files=changed_files, tool=result["tool"])
    return violations


def _run_sast_check(pcp_dir: Path, project_root: Path, changed_files: list[str], ctx: dict) -> list[str]:
    """SAST + secret-scan via semgrep, if installed. Scoped to changed files."""
    result = qa.run_sast(project_root, changed_files)
    violations: list[str] = []
    if result["tool"] and not result["passed"]:
        findings = "\n".join(result["findings"][:10])
        violations.append(f"SAST ({result['tool']}) found issues:\n{findings}")
    _qa_record(pcp_dir, ctx, "sast", violations, control_id="CTRL-003", files=changed_files, tool=result["tool"])
    return violations


def _run_architect_review(pcp_dir: Path, diff: str, changed_files: list[str], ctx: dict) -> list[str]:
    """Run architect review and return BLOCK findings."""
    from pcp.commands.architect_review import SYSTEM_PROMPT, _build_prompt, _load_persona, _load_kb
    persona = _load_persona(pcp_dir)
    architecture = (pcp_dir / "architecture.md").read_text() if (pcp_dir / "architecture.md").exists() else ""
    kb = _load_kb(pcp_dir, changed_files)

    prompt = _build_prompt(persona, architecture, kb, diff, "diff")
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
    _qa_record(pcp_dir, ctx, "architect-review", blocks, meta, control_id="CTRL-005", files=changed_files)
    return blocks


def _run_gate_check(pcp_dir: Path, diff: str, ctx: dict) -> list[str]:
    """Run gate review and return block issues."""
    from pcp.commands.gate import SYSTEM_PROMPT, _build_prompt, _load_llm_rules
    objective = (pcp_dir / "objective.md").read_text() if (pcp_dir / "objective.md").exists() else ""
    target_state = (pcp_dir / "target_state.md").read_text() if (pcp_dir / "target_state.md").exists() else ""
    current_state = (pcp_dir / "current_state.md").read_text() if (pcp_dir / "current_state.md").exists() else ""
    llm_rules = _load_llm_rules(pcp_dir)

    prompt = _build_prompt(objective, target_state, current_state, diff, llm_rules)
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
    _qa_record(pcp_dir, ctx, "gate", issues, meta, control_id="CTRL-006")
    return issues


def _record_escalation(pcp_dir: Path, module_name: str, criterion_id: str, block_findings: list[str]) -> None:
    """Route this criterion's final-attempt failure through OPA's escalation
    policy (.pcp/policies/escalation.rego) -- advisory only, doesn't change
    control flow (a 3rd-attempt failure already stops the build and hands
    back to a human either way). Confidence is a simple proxy: how many
    distinct gate categories still had violations on the last attempt.
    high_stakes fires on any SEC_* (secrets/eval/sql-injection) finding --
    those should never be treated as routine, low-stakes retries.

    Degrades silently if opa isn't installed or no escalation.rego is
    scaffolded -- this is informational, never a hard dependency on OPA."""
    from pcp import policy
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


def _build_one_criterion(
    pcp_dir: Path, project_root: Path, mod: dict, c: dict,
    build_model: str | None, budget: "_BuildBudget",
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
    agent_session_id = str(uuid.uuid4())

    for attempt in range(1, 4):
        console.print(f"\n[dim]Attempt {attempt}/3 — {mod['name']}/{c['id']}...[/dim]")

        try:
            budget.take_session()
        except BudgetExceeded:
            console.print(
                f"[red bold]Budget circuit breaker: exceeded {budget.max_sessions} agent "
                "sessions this run.[/red bold]"
            )
            console.print("[dim]Override with PCP_MAX_BUILD_SESSIONS=<n> if this build genuinely needs more.[/dim]")
            raise

        # First attempt opens a fresh session; retries --resume it instead of
        # cold-restarting (which re-explores the whole repo and re-pastes context).
        if attempt == 1:
            agent_prompt = _build_agent_prompt(pcp_dir, mod["name"], c, mod["spec"])
            session_flag = ["--session-id", agent_session_id]
        else:
            agent_prompt = _build_retry_prompt(feedback)
            session_flag = ["--resume", agent_session_id]

        cmd = [
            _claude_bin(),
            "-p",
            "--permission-mode", "acceptEdits",
            "--output-format", "json",
            "--max-budget-usd", _build_agent_max_budget_usd(),
            *session_flag,
        ]
        if build_model:
            cmd += ["--model", build_model]

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
            continue

        if result.returncode != 0:
            console.print("[red]Claude agent exited with error.[/red]")
            feedback = "Claude CLI agent run failed or exited with non-zero code."
            continue

        agent_usage = {}
        try:
            envelope = json.loads(result.stdout)
            if envelope.get("is_error"):
                console.print(f"[red]Claude agent reported an error:[/red] {envelope.get('result', '')}")
                feedback = f"Previous attempt errored: {envelope.get('result', '')}"
                continue
            with _STATE_LOCK:
                _log_usage(
                    pcp_dir, "build-agent", build_model, envelope.get("session_id"),
                    envelope.get("usage", {}), envelope.get("total_cost_usd"),
                )
            budget.add_cost(envelope.get("total_cost_usd"))
            agent_usage = {
                "model": build_model or "default",
                "session_id": envelope.get("session_id"),
                "usage": envelope.get("usage", {}),
                "cost_usd": envelope.get("total_cost_usd"),
                "duration_ms": envelope.get("duration_ms"),
            }
        except (json.JSONDecodeError, TypeError):
            pass

        # Run checks
        staged = _get_staged_files(project_root)
        unstaged = _get_unstaged_files(project_root)
        changed_files = list(set(staged + unstaged))

        if not changed_files:
            console.print("[yellow]No files were modified by the agent.[/yellow]")

        diff = _get_working_diff(project_root)

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
        ctx = {"module": mod["name"], "submodule": None, "criterion_id": c["id"], "attempt": attempt, "files": changed_files}
        violations_tests = _run_test_suite_check(pcp_dir, project_root, ctx)
        violations_lint = _run_lint_check(pcp_dir, project_root, changed_files, ctx)
        violations_sast = _run_sast_check(pcp_dir, project_root, changed_files, ctx)
        violations_l1 = _run_layer1_check(pcp_dir, project_root, changed_files, ctx)
        violations_arch = _run_architect_review(pcp_dir, diff, changed_files, ctx)
        violations_gate = _run_gate_check(pcp_dir, diff, ctx)

        block_findings = (
            violations_tests + violations_lint + violations_sast
            + violations_l1 + violations_arch + violations_gate
        )

        if block_findings:
            console.print(f"[red]BLOCKED by quality/architecture gates ({mod['name']}/{c['id']}):[/red]")
            for v in block_findings:
                console.print(f"  ✗ {v}")
            feedback = "\n".join(block_findings)
        else:
            success = True
            break

    return success, block_findings


def _build_module_worker(
    pcp_dir: Path, mod: dict, project_root: Path,
    build_model: str | None, budget: "_BuildBudget",
) -> dict:
    """Runs all of one module's pending criteria sequentially inside
    `project_root` (its own worktree when building in parallel). Stops at
    the first criterion that fails all 3 attempts. Never raises for a
    build/gate failure — only BudgetExceeded propagates, since that's a
    whole-run circuit breaker, not a per-module outcome."""
    console.print(f"\n[bold]Building Module:[/bold] [cyan]'{mod['name']}'[/cyan] ({len(mod['pending_criteria'])} pending criteria)")

    for c in mod["pending_criteria"]:
        console.print(f"\n[bold underline]Criterion [{c['id']}]:[/bold underline] {c['description']}")
        success, block_findings = _build_one_criterion(pcp_dir, project_root, mod, c, build_model, budget)

        if success:
            console.print(f"[green]✓ Criterion [{c['id']}] passed all gates successfully![/green]")
            # No cross-module contention (each module only ever touches its
            # own acceptance.yaml), but still guarded for consistency.
            with _STATE_LOCK:
                acc_data = load_yaml(mod["acc_path"])
                for crit in acc_data.get("criteria", []):
                    if crit["id"] == c["id"]:
                        crit["status"] = "complete"
                mod["acc_path"].write_text(yaml.dump(acc_data, default_flow_style=False))
        else:
            console.print(f"[red]✗ Failed to build Criterion [{c['id']}] after 3 attempts.[/red]")
            _record_escalation(pcp_dir, mod["name"], c["id"], block_findings)
            return {"module": mod["name"], "success": False, "failed_criterion": c["id"], "block_findings": block_findings}

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

    # Gather modules to run
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

    budget = _BuildBudget(_max_build_sessions())
    build_model = os.environ.get("PCP_BUILD_MODEL")
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
                result = _build_module_worker(pcp_dir, mod, project_root, build_model, budget)
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
                            _build_module_worker, pcp_dir, mod, worktrees[mod["name"]], build_model, budget,
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
                    ok, merge_output = _merge_module_branch(project_root, m_name)
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
