import stat
import subprocess
import textwrap
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.build import (
    _BuildBudget, BudgetExceeded, _setup_worktree, _merge_module_branch,
    _cleanup_worktree, _auto_commit_criterion,
)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _init_repo(tmp_path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    return tmp_path


# ── _BuildBudget thread-safety ──

def test_budget_allows_up_to_max_sessions():
    budget = _BuildBudget(max_sessions=3)
    budget.take_session()
    budget.take_session()
    budget.take_session()
    with pytest.raises(BudgetExceeded):
        budget.take_session()
    assert budget.session_count == 4


def test_budget_trips_correctly_under_real_concurrent_access():
    """20 threads all racing to take a session against a cap of 5 -- exactly
    5 must succeed silently and the rest must see BudgetExceeded, with no
    lost updates (final count must be exactly 21, one over the cap, since
    the thread that trips it still increments before raising)."""
    budget = _BuildBudget(max_sessions=5)
    exceeded_count = [0]
    succeeded_count = [0]
    lock = threading.Lock()

    def worker():
        try:
            budget.take_session()
            with lock:
                succeeded_count[0] += 1
        except BudgetExceeded:
            with lock:
                exceeded_count[0] += 1

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert succeeded_count[0] == 5
    assert exceeded_count[0] == 15
    assert budget.session_count == 20


def test_budget_add_cost_thread_safe():
    budget = _BuildBudget(max_sessions=1000)
    threads = [threading.Thread(target=lambda: budget.add_cost(0.01)) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert round(budget.run_cost_total, 2) == 0.50


# ── worktree helpers, real git ──

def test_setup_worktree_creates_new_worktree_and_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = _setup_worktree(repo, "add")
    try:
        assert wt.exists()
        assert (wt / "README.md").exists()
        branches = _git(["branch", "--list", "feat/add"], repo).stdout
        assert "feat/add" in branches
    finally:
        _cleanup_worktree(repo, "add", wt)


def test_setup_worktree_reuses_existing_dir(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt1 = _setup_worktree(repo, "add")
    wt2 = _setup_worktree(repo, "add")
    assert wt1 == wt2
    _cleanup_worktree(repo, "add", wt1)


def test_merge_module_branch_brings_in_the_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = _setup_worktree(repo, "add")
    (wt / "add.py").write_text("def add(a, b): return a + b\n")
    _git(["add", "add.py"], wt)
    _git(["commit", "-q", "-m", "feat: add"], wt)

    ok, output = _merge_module_branch(repo, "add")
    assert ok, output
    assert (repo / "add.py").exists()
    _cleanup_worktree(repo, "add", wt)


def test_cleanup_worktree_removes_worktree_and_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    wt = _setup_worktree(repo, "add")
    (wt / "add.py").write_text("x = 1\n")
    _git(["add", "add.py"], wt)
    _git(["commit", "-q", "-m", "feat: add"], wt)
    _merge_module_branch(repo, "add")

    _cleanup_worktree(repo, "add", wt)
    assert not wt.exists()
    branches = _git(["branch", "--list", "feat/add"], repo).stdout
    assert "feat/add" not in branches


# ── real end-to-end concurrency proof ──

FAKE_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import sys, os, re, json, time, subprocess

    sleep_s = float(os.environ.get("PCP_TEST_AGENT_SLEEP", "0.6"))
    prompt = sys.stdin.read()
    m = re.search(r"Criterion: \\[(\\w+)\\]", prompt)
    crit_id = m.group(1) if m else "X"

    start_log = os.environ.get("PCP_TEST_TIMING_LOG")
    if start_log:
        with open(start_log, "a") as f:
            f.write(f"{crit_id} start {time.time()}\\n")

    time.sleep(sleep_s)

    fname = f"{crit_id}.txt"
    with open(fname, "w") as f:
        f.write("implemented\\n")
    subprocess.run(["git", "add", fname])
    subprocess.run(["git", "commit", "-m", f"feat: {crit_id}"], capture_output=True)

    if start_log:
        with open(start_log, "a") as f:
            f.write(f"{crit_id} end {time.time()}\\n")

    envelope = {
        "is_error": False, "result": "done", "session_id": f"fake-{crit_id}",
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.001,
        "duration_ms": int(sleep_s * 1000),
    }
    print(json.dumps(envelope))
""")


def _write_module(pcp_dir, name, criterion_id):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {"version": "1.0", "module": name, "description": f"module {name} does something.",
            "objective_coverage": ["x"], "dependencies": [], "constraints": []}
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    acc = {"version": "1.0", "module": name, "criteria": [
        {"id": criterion_id, "description": "core impl", "check": "manual", "status": "pending"},
    ]}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))
    return mod_dir


@pytest.fixture
def parallel_project(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    _write_module(pcp_dir, "add", "A001")
    _write_module(pcp_dir, "sub", "S001")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    fake_agent = tmp_path / "fake_claude.py"
    fake_agent.write_text(FAKE_AGENT)
    fake_agent.chmod(fake_agent.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("PCP_CLAUDE_BIN", str(fake_agent))
    monkeypatch.setenv("PCP_TEST_AGENT_SLEEP", "0.6")
    timing_log = tmp_path / "timing.log"
    monkeypatch.setenv("PCP_TEST_TIMING_LOG", str(timing_log))
    monkeypatch.setenv("PCP_BUILD_MAX_PARALLEL", "2")

    return repo, pcp_dir, timing_log


def test_two_independent_modules_build_concurrently(parallel_project):
    repo, pcp_dir, timing_log = parallel_project

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]), \
         patch("pcp.commands.build.qa.run_test_suite", return_value={"tool": None, "passed": True, "output": ""}), \
         patch("pcp.commands.validate_strategy.run_validate_strategy", return_value=None), \
         patch("pcp.llm.client.call_json", return_value={"findings": []}):
        # The three patches above are wave-merge's own real work (integration
        # test suite, validate-strategy, architect-review) -- unrelated to
        # the per-criterion parallelism this test is proving, and each would
        # otherwise re-invoke the fake agent (0.6s apiece), swamping the
        # timing signal below with sequential, once-per-wave overhead.
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "building 2 module(s) in parallel" in result.output

    # Both modules' commits actually landed on main (proves worktree + merge worked).
    assert (repo / "A001.txt").exists()
    assert (repo / "S001.txt").exists()

    # No leftover worktrees/branches after a clean run.
    worktree_list = _git(["worktree", "list"], repo).stdout
    assert "repo-add" not in worktree_list
    assert "repo-sub" not in worktree_list
    branches = _git(["branch"], repo).stdout
    assert "feat/add" not in branches
    assert "feat/sub" not in branches

    # Both acceptance criteria marked complete.
    add_acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "add" / "acceptance.yaml").read_text())
    sub_acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "sub" / "acceptance.yaml").read_text())
    assert add_acc["criteria"][0]["status"] == "complete"
    assert sub_acc["criteria"][0]["status"] == "complete"

    # Timing log proves real overlap: module B's agent started before module A's finished.
    lines = timing_log.read_text().splitlines()
    events = {}
    for line in lines:
        crit, phase, ts = line.split()
        events.setdefault(crit, {})[phase] = float(ts)
    a_start, a_end = events["A001"]["start"], events["A001"]["end"]
    s_start, s_end = events["S001"]["start"], events["S001"]["end"]
    overlap = min(a_end, s_end) - max(a_start, s_start)
    assert overlap > 0, f"no timing overlap between the two agent runs: {events}"


def test_single_module_wave_never_uses_worktrees(parallel_project, tmp_path):
    """A wave with only one module should run directly against the main
    project root -- no worktree machinery, no parallel-build banner."""
    repo, pcp_dir, timing_log = parallel_project

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--module", "add", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "in parallel" not in result.output
    assert (repo / "A001.txt").exists()
    worktree_list = _git(["worktree", "list"], repo).stdout
    assert "repo-add" not in worktree_list


def test_module_filter_nudges_when_other_modules_pending(parallel_project):
    """--module against a project with other pending modules should print
    the self-reporting nudge with the correct other-module count, so a
    human/orchestrator sees the wave-parallelism headroom they're trading
    away instead of discovering it later."""
    repo, pcp_dir, timing_log = parallel_project

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--module", "add", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "1 other module(s) also have pending criteria" in result.output
    assert "pcp build` with no `--module` filter" in result.output


def test_no_module_filter_never_nudges(parallel_project):
    """A plain `pcp build` (no --module) already builds every pending
    module in its wave -- there is nothing being traded away, so the
    nudge must not fire."""
    repo, pcp_dir, timing_log = parallel_project

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]), \
         patch("pcp.commands.build.qa.run_test_suite", return_value={"tool": None, "passed": True, "output": ""}), \
         patch("pcp.commands.validate_strategy.run_validate_strategy", return_value=None), \
         patch("pcp.llm.client.call_json", return_value={"findings": []}):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "other module(s) also have pending criteria" not in result.output


def test_module_filter_no_nudge_when_it_is_the_only_pending_module(parallel_project):
    """--module against a project where every OTHER module is already
    complete has nothing to trade away either -- nudge must stay silent."""
    repo, pcp_dir, timing_log = parallel_project
    sub_acc_path = pcp_dir / "strategy" / "modules" / "sub" / "acceptance.yaml"
    sub_acc = yaml.safe_load(sub_acc_path.read_text())
    sub_acc["criteria"][0]["status"] = "complete"
    sub_acc_path.write_text(yaml.dump(sub_acc))
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "sub already done"], repo)

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--module", "add", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "other module(s) also have pending criteria" not in result.output


# ── Stale-base + agent-local-config merge hazards (2026-07-25) ──

def test_setup_worktree_syncs_a_stale_reused_branch_to_current_base(tmp_path):
    """A feat/ branch left over from an earlier run is arbitrarily far behind
    main, and every commit main gained since is conflict surface at merge
    time. Setup must reconcile it at the START of the criterion, not leave it
    for _merge_module_branch to report as an unresolvable conflict."""
    repo = _init_repo(tmp_path / "repo")
    _git(["branch", "feat/stale"], repo)

    # main moves on after the branch was cut.
    (repo / "added_on_main.txt").write_text("main work\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "main moves on"], repo)

    wt = _setup_worktree(repo, "stale")
    assert (wt / "added_on_main.txt").exists(), "reused branch was left on a stale base"


def test_setup_worktree_leaves_a_dirty_reused_worktree_alone(tmp_path):
    """An interrupted run's uncommitted work must never be clobbered by the
    base sync — skip the merge and warn instead."""
    repo = _init_repo(tmp_path / "repo")
    wt = _setup_worktree(repo, "dirty")
    (wt / "in_progress.txt").write_text("half-written agent work\n")

    (repo / "added_on_main.txt").write_text("main work\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "main moves on"], repo)

    again = _setup_worktree(repo, "dirty")
    assert again == wt
    assert (wt / "in_progress.txt").read_text() == "half-written agent work\n"


def test_setup_worktree_merge_is_a_noop_when_already_current(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    wt = _setup_worktree(repo, "fresh")
    assert _git(["rev-parse", "HEAD"], wt).stdout.strip() == head_before


def test_auto_commit_never_commits_agent_session_local_config(tmp_path):
    """Claude Code writes .claude/settings*.json into whatever directory it
    runs in, scoped to THAT directory. Committing it makes every wave merge an
    add/add conflict on a scratch config file."""
    repo = _init_repo(tmp_path / "repo")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"env": {"TMPDIR": "/wt-a"}}')
    (repo / ".claude" / "settings.local.json").write_text('{"permissions": []}')
    (repo / "real_work.py").write_text("def f(): return 1\n")

    _auto_commit_criterion(repo, "mod", {"id": "A001", "description": "do the thing"})

    tracked = _git(["ls-files"], repo).stdout.split()
    assert "real_work.py" in tracked
    assert ".claude/settings.json" not in tracked
    assert ".claude/settings.local.json" not in tracked


# ── Gates must fail closed, merges must leave no wreckage (2026-07-27) ──

def test_gate_infrastructure_failure_blocks_by_default():
    """A gate that could not run is not a gate that passed. Returning [] here
    marked un-reviewed criteria complete and merged them."""
    from pcp.commands.build import _gate_infrastructure_failure
    findings = _gate_infrastructure_failure("gate", RuntimeError("429 rate limited"))
    assert findings, "an un-runnable gate must produce a blocking finding"
    assert "429 rate limited" in findings[0]
    assert "infrastructure failure" in findings[0].lower()


def test_gate_infrastructure_failure_opt_out_is_explicit(monkeypatch):
    from pcp.commands.build import _gate_infrastructure_failure
    monkeypatch.setenv("PCP_ALLOW_UNVERIFIED_GATES", "1")
    assert _gate_infrastructure_failure("gate", RuntimeError("boom")) == []


def test_gate_infrastructure_failure_opt_out_requires_exactly_1(monkeypatch):
    """A truthy-looking value must not silently disable a gate."""
    from pcp.commands.build import _gate_infrastructure_failure
    for value in ("0", "true", "yes", ""):
        monkeypatch.setenv("PCP_ALLOW_UNVERIFIED_GATES", value)
        assert _gate_infrastructure_failure("gate", RuntimeError("boom")), \
            f"value {value!r} must not disable the gate"


def test_failed_merge_leaves_no_half_merged_repo(tmp_path):
    """Without `git merge --abort`, one conflicted criterion left project_root
    mid-MERGE with conflict markers, and every later git operation in the run
    failed on unmerged paths -- taking down criteria that had already passed."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "shared.txt").write_text("base\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "base"], repo)

    # Never hardcode "main": `git init`'s default branch follows the ambient
    # init.defaultBranch, which is "master" on stock git and in CI. Hardcoding
    # it made `git checkout main` silently no-op (_git does not check the
    # return code), so both edits landed on one branch, no conflict occurred,
    # and the test passed locally while failing in CI.
    base_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()

    # The branch and the base both edit the same line -> guaranteed conflict.
    _git(["checkout", "-q", "-b", "feat/conflicting"], repo)
    (repo / "shared.txt").write_text("from branch\n")
    _git(["commit", "-qam", "branch edit"], repo)
    assert _git(["checkout", "-q", base_branch], repo).returncode == 0
    (repo / "shared.txt").write_text("from base\n")
    _git(["commit", "-qam", "base edit"], repo)

    ok, _output = _merge_module_branch(repo, "conflicting")
    assert ok is False

    # The repo must be usable afterwards, not stuck mid-merge.
    assert not (repo / ".git" / "MERGE_HEAD").exists(), "left mid-MERGE"
    assert "<<<<<<<" not in (repo / "shared.txt").read_text(), "conflict markers left in tree"
    status = _git(["status", "--porcelain"], repo).stdout
    assert not [ln for ln in status.splitlines() if ln.startswith(("UU", "AA", "DD"))], \
        f"unmerged paths remain: {status}"
    # The base branch's own commit must be intact -- aborting cleans up the
    # half-merge, it does not revert work that was already committed.
    assert (repo / "shared.txt").read_text() == "from base\n"


# ── Audit-trail honesty + install-only SAST (2026-07-27) ──

def test_advisory_check_with_findings_is_not_recorded_as_pass(tmp_path):
    """Eleven wave checks pass result="pass" explicitly to mean "don't block
    the wave". Recording that literally made telemetry.jsonl — which `pcp
    provenance` reads directly — report a clean pass no matter what they
    found. A tool selling audit-grade evidence must not falsify its own."""
    from pcp.commands.build import _wave_record
    import json

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    _wave_record(pcp_dir, 1, "nav-depth", "CTRL-025", ["nav is 5 deep"], files=["a.tsx"], result="pass")
    _wave_record(pcp_dir, 1, "menu-bar", "CTRL-027", [], files=["b.tsx"], result="pass")

    records = [json.loads(ln) for ln in (pcp_dir / "telemetry.jsonl").read_text().splitlines() if ln.strip()]
    found = {r["check"]: r for r in records}

    assert found["wave-nav-depth"]["result"] == "advisory", "a check that found something is not a pass"
    assert found["wave-nav-depth"]["error_count"] == 1
    assert found["wave-menu-bar"]["result"] == "pass", "a genuinely clean check stays a pass"


def test_wave_record_default_and_block_paths_unchanged(tmp_path):
    from pcp.commands.build import _wave_record
    import json

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _wave_record(pcp_dir, 1, "contract", "CTRL-007", ["dep incomplete"])      # no result= -> block
    _wave_record(pcp_dir, 1, "test-suite", "CTRL-001", [])                     # no result= -> pass
    _wave_record(pcp_dir, 1, "sast", "CTRL-003", ["x"], result="skipped")      # explicit non-pass preserved

    r = {json.loads(ln)["check"]: json.loads(ln) for ln in
         (pcp_dir / "telemetry.jsonl").read_text().splitlines() if ln.strip()}
    assert r["wave-contract"]["result"] == "block"
    assert r["wave-test-suite"]["result"] == "pass"
    assert r["wave-sast"]["result"] == "skipped", "an explicit non-pass result must not be rewritten"


def test_provenance_renders_advisory_result():
    """A result value telemetry can emit but provenance cannot render would
    show up as a blank cell in the audit document."""
    from pcp.commands.provenance import RESULT_SYMBOL
    assert "advisory" in RESULT_SYMBOL


def test_install_only_scans_what_it_installed():
    """The fast path exists to pull in THIRD-PARTY code on a human's say-so —
    the likeliest supply-chain entry point in the whole tool — and it was the
    one path that skipped the secret/SAST scan."""
    import inspect
    from pcp.commands.build import _run_install_only
    src = inspect.getsource(_run_install_only)
    assert "_run_sast_check" in src
    assert "_run_layer1_check" in src
    assert "_run_test_suite_check" in src


def test_pcp_own_progress_file_is_not_agent_output():
    """_write_progress writes this on every attempt; if it is not classified
    as operational it lands in changed_files, pollutes the judge diff and
    draws scope-guard findings against the agent — the exact 2026-07-17 bug."""
    from pcp.commands.build import _is_pcp_operational
    assert _is_pcp_operational(".pcp/build_progress.yaml")
    assert _is_pcp_operational("./.pcp/build_progress.yaml")
    assert not _is_pcp_operational("src/app/main.py")
    # Module specs are deliberately NOT operational -- they are real deliverables.
    assert not _is_pcp_operational(".pcp/strategy/modules/api/spec.yaml")


# ── Untracked work must be visible to the gates (2026-07-27 signtool dogfood) ──

def test_working_diff_includes_brand_new_untracked_files(tmp_path):
    """`git diff` never shows untracked files. An agent that creates only NEW
    files and leaves them unstaged produced an empty diff, so the alignment
    gate returned "No diff provided; cannot assess alignment" and blocked at
    0% on work that plainly existed. Observed live on
    pdf-document-storage/A004, where the scope guard listed 3 modified files
    in the very same attempt."""
    from pcp.commands.build import _get_working_diff, _get_changed_files_since

    repo = _init_repo(tmp_path / "repo")
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    (repo / "brand_new.py").write_text("def handler():\n    return 'implemented'\n")

    files = _get_changed_files_since(repo, base)
    diff = _get_working_diff(repo, base)

    assert "brand_new.py" in files, "precondition: the file list already saw it"
    assert "brand_new.py" in diff, "the diff the gates judge must see it too"
    assert "implemented" in diff, "diff must carry the actual content, not just the path"


def test_working_diff_still_covers_tracked_and_committed_changes(tmp_path):
    from pcp.commands.build import _get_working_diff

    repo = _init_repo(tmp_path / "repo")
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    (repo / "committed.py").write_text("x = 1\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "agent committed its work"], repo)
    (repo / "README.md").write_text("edited unstaged\n")

    diff = _get_working_diff(repo, base)
    assert "committed.py" in diff, "committed work must stay visible"
    assert "edited unstaged" in diff, "unstaged edits must stay visible"


def test_working_diff_excludes_pcp_operational_untracked_writes(tmp_path):
    """PCP's own bookkeeping must not leak into the judge diff just because it
    arrives untracked -- that was the 2026-07-17 token-ledger bug."""
    from pcp.commands.build import _get_working_diff

    repo = _init_repo(tmp_path / "repo")
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / ".pcp").mkdir()
    (repo / ".pcp" / "build_progress.yaml").write_text("step: coding\n")
    (repo / "real_work.py").write_text("y = 2\n")

    diff = _get_working_diff(repo, base)
    assert "real_work.py" in diff
    assert "build_progress" not in diff


def test_auto_commit_never_stages_pcp_operational_writes(tmp_path):
    """PCP appends to token_ledger/telemetry in the MAIN .pcp/ throughout a
    build. If a worktree branch commits them, the merge home fails with "your
    local changes would be overwritten by merge" — main has uncommitted edits
    to a tracked file the branch also committed. Halted the signtool dogfood
    on criterion A001 after A002/A004 had already merged cleanly."""
    repo = _init_repo(tmp_path / "repo")
    pcp = repo / ".pcp"
    (pcp / "evidence" / "mod" / "A1").mkdir(parents=True)
    (pcp / "token_ledger.yaml").write_text("calls: []\n")
    (pcp / "telemetry.jsonl").write_text('{"x":1}\n')
    (pcp / "build_progress.yaml").write_text("step: coding\n")
    (pcp / "evidence" / "mod" / "A1" / "gate.txt").write_text("raw\n")
    (pcp / "objective.md").write_text("# Objective\n")   # a governance SPEC
    (repo / "real_work.py").write_text("def f(): return 1\n")

    _auto_commit_criterion(repo, "mod", {"id": "A001", "description": "d"})

    tracked = _git(["ls-files"], repo).stdout.split()
    assert "real_work.py" in tracked
    assert ".pcp/objective.md" in tracked, "governance specs must still be committed"
    for path in (".pcp/token_ledger.yaml", ".pcp/telemetry.jsonl",
                 ".pcp/build_progress.yaml", ".pcp/evidence/mod/A1/gate.txt"):
        assert path not in tracked, f"{path} must never be auto-committed"


def test_auto_commit_excludes_derive_from_the_operational_tuples(tmp_path):
    """Naming offending files one at a time is what let this bug return twice.
    The exclude list must stay derived from _PCP_OPERATIONAL_PATHS so a file
    added there is covered here automatically."""
    from pcp.commands.build import (
        _AUTO_COMMIT_EXCLUDES, _PCP_OPERATIONAL_PATHS, _PCP_OPERATIONAL_DIRS,
    )
    for p in _PCP_OPERATIONAL_PATHS:
        assert f":!{p}" in _AUTO_COMMIT_EXCLUDES
    for d in _PCP_OPERATIONAL_DIRS:
        assert f":!{d.rstrip('/')}" in _AUTO_COMMIT_EXCLUDES
    assert ":!.claude/settings.json" in _AUTO_COMMIT_EXCLUDES


def test_no_unregistered_pcp_runtime_writer():
    """Structural guard, not a spot fix.

    Five separate bugs this session traced to one shape: a fix applied to a
    call site instead of to the rule. The .pcp/ runtime-writer case bit twice
    in an hour — build_progress.yaml (added 07-24) and run_ledger.jsonl (added
    07-23) were each written on every build while missing from
    _PCP_OPERATIONAL_PATHS, so they polluted the judge diff, drew scope-guard
    findings against the agent, and got committed by worktree branches where
    they broke the merge home.

    So: scan the source for anything writing a file into pcp_dir, and require
    it to be either registered as operational or an allowlisted governance
    spec. A new writer added without a decision fails here instead of in
    someone's build weeks later."""
    import re
    from pcp.commands.build import _PCP_OPERATIONAL_PATHS, _PCP_OPERATIONAL_DIRS

    # Files NOT written during a build, so correctly absent from the
    # operational set. Two groups: human-authorized specs / scaffolded config,
    # and artifacts produced by one-time or explicitly-invoked commands
    # (`pcp import`, `pcp provenance --attest`) that no build ever touches.
    # Verified 2026-07-27: none of the second group is reachable from build.py.
    NOT_BUILD_TIME = {
        "baseline_violations.yaml",   # pcp import scaffold + pcp check --baseline
        "discovery_graph.json",       # pcp import, one-time
        "attestations.meta.json",     # pcp provenance --attest
        "ontology_state.yaml",        # pcp_dir path helper, ontology-foundry integration
        "objective.md", "target_state.md", "architecture.md", "ci_rules.yaml",
        "controls.yaml", "SDLC_phase.yaml", "context_map.yaml", "design_system.md",
        "design_conventions.yaml", "ui_kit_recipes.yaml", "logic_tier_guide.md",
        "architect_persona.md", "RECOMMENDED_PERMISSIONS.md", "bypass_log.yaml",
        "install_approvals.yaml", "attestations.jsonl", "audit_trend.jsonl",
        "pressure_test_log.jsonl", "symbol_fingerprints.json", "integrations.yaml",
        "deploy_log.yaml", "narrative_lint.md", "audit.md", "provenance.md",
        "architecture_justification.md", "design_audit.md", "control_audit.md",
        "build_report.md", "dashboard.html", "pcp.md",
    }
    registered = {p.removeprefix(".pcp/") for p in _PCP_OPERATIONAL_PATHS}
    registered_dirs = {d.removeprefix(".pcp/").rstrip("/") for d in _PCP_OPERATIONAL_DIRS}

    pattern = re.compile(r'pcp_dir\s*/\s*"([A-Za-z0-9_.-]+\.(?:jsonl|yaml|md|json))"')
    unregistered = {}
    for src in Path("src/pcp").rglob("*.py"):
        for name in pattern.findall(src.read_text()):
            if name in registered or name in NOT_BUILD_TIME:
                continue
            if any(name.startswith(d) for d in registered_dirs):
                continue
            unregistered.setdefault(name, []).append(str(src))

    assert not unregistered, (
        "New .pcp/ file(s) written by PCP but neither registered in "
        "_PCP_OPERATIONAL_PATHS nor allowlisted as a governance spec: "
        f"{unregistered}. Decide which it is — an operational write must be "
        "registered or it will pollute the judge diff and break wave merges."
    )


# ── File-scope wave partitioning (the 2026-07-27 collision, fixed) ──

def _crit(cid, target=None, **kw):
    c = {"id": cid, "description": f"criterion {cid}", "status": "pending"}
    if target:
        c["target"] = target
    c.update(kw)
    return c


def test_undeclared_criteria_run_in_parallel_optimistically(monkeypatch):
    """Corrected the same day it shipped. The first version demanded proof of
    disjointness via `target`, which serialised 237 ontology-foundry criteria
    that had opted into parallelism -- only 51 of 382 declare a target. A 15x
    throughput loss to prevent a collision that costs one rebuild and that
    `git merge --abort` already makes clean."""
    monkeypatch.delenv("PCP_CRITERIA_PARALLEL_STRICT", raising=False)
    from pcp.commands.build import _partition_wave_by_file_scope

    waves = _partition_wave_by_file_scope([_crit("A001"), _crit("A002"), _crit("A004")])
    assert len(waves) == 1, "undeclared criteria must fan out, not serialise"
    assert [c["id"] for c in waves[0]] == ["A001", "A002", "A004"]


def test_strict_mode_restores_prove_or_serialise(monkeypatch):
    monkeypatch.setenv("PCP_CRITERIA_PARALLEL_STRICT", "1")
    from pcp.commands.build import _partition_wave_by_file_scope
    waves = _partition_wave_by_file_scope([_crit("A001"), _crit("A002")])
    assert len(waves) == 2


def test_known_collisions_are_still_separated_up_front(monkeypatch):
    """Declared targets still earn something: two criteria that both declare
    the SAME file are known to collide before either runs, so they are split
    rather than discovered at merge time."""
    monkeypatch.delenv("PCP_CRITERIA_PARALLEL_STRICT", raising=False)
    from pcp.commands.build import _partition_wave_by_file_scope
    waves = _partition_wave_by_file_scope([
        _crit("A001", target="src/shared.py"),
        _crit("A002", target="src/shared.py"),
    ])
    ids = [[c["id"] for c in w] for w in waves]
    for w in ids:
        assert not ("A001" in w and "A002" in w)


def test_merge_collision_triggers_rebuild_not_module_failure():
    """The collision is recoverable: the merge aborts cleanly, so the criterion
    is rebuilt against the updated base rather than stopping the module and
    handing a human a git conflict."""
    import inspect
    from pcp.commands import build
    src = inspect.getsource(build._build_module_worker)
    assert "collided on merge" in src
    assert "_build_one_criterion" in src, "must rebuild, not just report"
    # bounded: a second failure after rebuild still fails the module
    assert "still could not be merged" in src


def test_distinct_declared_targets_still_run_in_parallel(monkeypatch):
    """The fix must not kill parallelism outright — declaring distinct targets
    is what buys it back."""
    monkeypatch.delenv("PCP_CRITERIA_PARALLEL_STRICT", raising=False)
    from pcp.commands.build import _partition_wave_by_file_scope

    waves = _partition_wave_by_file_scope([
        _crit("A001", target="src/a.py"),
        _crit("A002", target="src/b.py"),
        _crit("A003", target="src/c.py"),
    ])
    assert len(waves) == 1
    assert [c["id"] for c in waves[0]] == ["A001", "A002", "A003"]


def test_same_declared_target_is_serialised(monkeypatch):
    monkeypatch.delenv("PCP_CRITERIA_PARALLEL_STRICT", raising=False)
    from pcp.commands.build import _partition_wave_by_file_scope

    waves = _partition_wave_by_file_scope([
        _crit("A001", target="src/shared.py"),
        _crit("A002", target="src/other.py"),
        _crit("A003", target="src/shared.py"),
    ])
    ids = [[c["id"] for c in w] for w in waves]
    # A003 collides with A001, so it cannot be in the same sub-wave.
    for w in ids:
        assert not ("A001" in w and "A003" in w)
    assert sum(len(w) for w in ids) == 3, "no criterion may be dropped"


def test_no_criterion_is_ever_dropped_or_duplicated(monkeypatch):
    monkeypatch.delenv("PCP_CRITERIA_PARALLEL_STRICT", raising=False)
    from pcp.commands.build import _partition_wave_by_file_scope

    crits = [
        _crit("A001"), _crit("A002", target="src/a.py"),
        _crit("A003", target="src/a.py"), _crit("A004", target="src/b.py"),
        _crit("A005"),
    ]
    waves = _partition_wave_by_file_scope(crits)
    flat = [c["id"] for w in waves for c in w]
    assert sorted(flat) == ["A001", "A002", "A003", "A004", "A005"]
    assert len(flat) == len(set(flat))





def test_kickoff_prompt_no_longer_endorses_merge_time_collisions():
    """The prompt used to tell the model to 'let the two criteria's own
    file-level conflicts surface at merge time' — actively instructing the
    failure mode that halted the signtool build."""
    from pcp.commands import kickoff
    src = Path(kickoff.__file__).read_text()
    assert "surface at merge time" not in src
    assert "target" in src and "targets differ" in src


# ── A criterion gate tests the PRODUCT, not PCP's paperwork (2026-07-27) ──

def test_per_criterion_gates_do_not_grade_declarations():
    """Measured on ontology-foundry: 35% of 1,632 gate executions checked
    declarations rather than the product, and those produced 108 of 187 blocks
    — 58%. 97 were the scope guard objecting that files fell outside a surface
    derived from `target`, which 331 of 382 criteria never declared: PCP
    blocking on its own missing metadata, charged to every attempt.

    Nothing that grades declaration TEXT or declared file surfaces belongs in
    the build's hot path. This test exists because removing them broke no
    existing test — nothing pinned the gate set."""
    import inspect
    from pcp.commands import build

    src = inspect.getsource(build._build_one_criterion)
    gate_block = src[src.index("gate_calls = {"):src.index("with ThreadPoolExecutor")]

    for paperwork in ('"scope"', '"design_justification"',
                      '"bvb_justification"', '"customization"'):
        assert paperwork not in gate_block, (
            f"{paperwork} grades a declaration, not the product — it must not run "
            f"per criterion. Put it in `pcp audit` if the reporting is wanted."
        )


def test_per_criterion_gates_still_cover_the_product():
    """The removal must not take real product checks with it."""
    import inspect
    from pcp.commands import build

    src = inspect.getsource(build._build_one_criterion)
    gate_block = src[src.index("gate_calls = {"):src.index("with ThreadPoolExecutor")]

    for product in ('"tests"', '"lint"', '"sast"', '"l1"', '"arch"', '"gate"',
                    '"a11y"', '"visual_quality"', '"lazy_marker"',
                    '"design_consistency"'):
        assert product in gate_block, f"{product} tests the built product and must stay"


def test_only_product_failures_block_a_criterion():
    """A criterion fails on: tests, lint, SAST, ci_rules, or review of the diff.
    Not on how well its build_vs_buy rationale reads."""
    import inspect
    from pcp.commands import build

    src = inspect.getsource(build._build_one_criterion)
    start = src.index("block_findings = (")
    blocking = src[start:src.index(")", start)]

    for product in ("tests", "lint", "sast", "l1", "arch", "gate"):
        assert f'gate_results["{product}"]' in blocking

    for paperwork in ("scope", "design_justification", "bvb_justification", "customization"):
        assert f'gate_results["{paperwork}"]' not in blocking, (
            f"{paperwork} must not be able to fail a criterion"
        )
