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
    _cleanup_worktree,
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
