import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.build import _criteria_parallel_enabled, _compute_criterion_waves


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


# ── opt-in signal + wave computation, pure logic ──

def test_no_depends_on_anywhere_means_disabled():
    mod = {"pending_criteria": [
        {"id": "A001", "description": "x"},
        {"id": "A002", "description": "y"},
    ]}
    assert _criteria_parallel_enabled(mod) is False


def test_empty_depends_on_list_still_counts_as_opt_in():
    """Presence of the key is the signal, not its content -- an empty list
    is a deliberate 'this has no deps' declaration, not absence."""
    mod = {"pending_criteria": [
        {"id": "A001", "description": "x", "depends_on": []},
        {"id": "A002", "description": "y"},
    ]}
    assert _criteria_parallel_enabled(mod) is True


def test_waves_independent_criteria_all_wave_zero():
    mod = {"pending_criteria": [
        {"id": "A001", "depends_on": []},
        {"id": "A002", "depends_on": []},
        {"id": "A003", "depends_on": []},
    ]}
    waves = _compute_criterion_waves(mod)
    assert waves == {"A001": 0, "A002": 0, "A003": 0}


def test_waves_respect_chain_dependency():
    mod = {"pending_criteria": [
        {"id": "A001", "depends_on": []},
        {"id": "A002", "depends_on": ["A001"]},
        {"id": "A003", "depends_on": ["A002"]},
    ]}
    waves = _compute_criterion_waves(mod)
    assert waves == {"A001": 0, "A002": 1, "A003": 2}


def test_waves_two_independent_branches_off_common_root():
    mod = {"pending_criteria": [
        {"id": "A001", "depends_on": []},
        {"id": "A002", "depends_on": ["A001"]},
        {"id": "A003", "depends_on": ["A001"]},
    ]}
    waves = _compute_criterion_waves(mod)
    assert waves["A001"] == 0
    assert waves["A002"] == 1
    assert waves["A003"] == 1


def test_dependency_on_already_complete_criterion_is_satisfied():
    """A dep pointing outside this run's pending set (already complete, or
    external) is treated as already satisfied -- same rule module-level
    _compute_waves uses for a dependency outside the current build's module
    set."""
    mod = {"pending_criteria": [
        {"id": "A002", "depends_on": ["A001"]},  # A001 not in pending set
    ]}
    waves = _compute_criterion_waves(mod)
    assert waves == {"A002": 0}


def test_circular_dependency_does_not_infinite_loop():
    """Same base-case semantics as module-level _compute_waves: the second
    encounter of a node already 'in progress' (in `seen`) returns 0 rather
    than recursing forever -- it does not force the WHOLE cycle to wave 0,
    just breaks the recursion. What matters here is only that this
    terminates and returns a wave for every criterion."""
    mod = {"pending_criteria": [
        {"id": "A001", "depends_on": ["A002"]},
        {"id": "A002", "depends_on": ["A001"]},
    ]}
    waves = _compute_criterion_waves(mod)
    assert set(waves) == {"A001", "A002"}
    assert all(isinstance(w, int) for w in waves.values())


# ── end-to-end: default stays sequential, opt-in actually overlaps ──

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


def _write_module(pcp_dir, name, criteria):
    """criteria: list of (id, depends_on_or_None)."""
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {"version": "1.0", "module": name, "description": f"module {name} does something.",
            "objective_coverage": ["x"], "dependencies": [], "constraints": []}
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    crit_entries = []
    for cid, deps in criteria:
        entry = {"id": cid, "description": "core impl", "check": "manual", "status": "pending"}
        if deps is not None:
            entry["depends_on"] = deps
        crit_entries.append(entry)
    acc = {"version": "1.0", "module": name, "criteria": crit_entries}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))
    return mod_dir


def _fake_claude(tmp_path, monkeypatch, timing_log, sleep_s="0.6"):
    fake_agent = tmp_path / "fake_claude.py"
    fake_agent.write_text(FAKE_AGENT)
    fake_agent.chmod(fake_agent.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PCP_CLAUDE_BIN", str(fake_agent))
    monkeypatch.setenv("PCP_TEST_AGENT_SLEEP", sleep_s)
    monkeypatch.setenv("PCP_TEST_TIMING_LOG", str(timing_log))


_GATE_PATCHES = dict(
    test_suite_check=[], lint_check=[], sast_check=[], layer1_check=[],
    architect_review=[], gate_check=[],
)


def test_default_module_without_depends_on_stays_sequential_no_worktrees(tmp_path, monkeypatch):
    """Regression check: a module where no criterion declares depends_on
    must build exactly as before -- no worktree ever created for a
    criterion, no wave banner printed."""
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    _write_module(pcp_dir, "add", [("A001", None), ("A002", None)])
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    timing_log = tmp_path / "timing.log"
    _fake_claude(tmp_path, monkeypatch, timing_log)

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
    assert (repo / "A002.txt").exists()
    worktree_list = _git(["worktree", "list"], repo).stdout
    assert "repo-add-A001" not in worktree_list
    assert "repo-add-A002" not in worktree_list

    acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "add" / "acceptance.yaml").read_text())
    assert {c["id"]: c["status"] for c in acc["criteria"]} == {"A001": "complete", "A002": "complete"}


def test_opted_in_independent_criteria_build_concurrently_in_worktrees(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    _write_module(pcp_dir, "add", [("A001", []), ("A002", [])])
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    timing_log = tmp_path / "timing.log"
    _fake_claude(tmp_path, monkeypatch, timing_log)

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--module", "add", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "Criterion wave 0: 2 independent criteria in 'add' building in parallel" in result.output
    assert (repo / "A001.txt").exists()
    assert (repo / "A002.txt").exists()

    # No leftover worktrees/branches after a clean run.
    worktree_list = _git(["worktree", "list"], repo).stdout
    assert "repo-add-A001" not in worktree_list
    assert "repo-add-A002" not in worktree_list
    branches = _git(["branch"], repo).stdout
    assert "feat/add-A001" not in branches
    assert "feat/add-A002" not in branches

    acc = yaml.safe_load((pcp_dir / "strategy" / "modules" / "add" / "acceptance.yaml").read_text())
    assert {c["id"]: c["status"] for c in acc["criteria"]} == {"A001": "complete", "A002": "complete"}

    # Timing log proves real overlap between the two criteria's agent runs.
    lines = timing_log.read_text().splitlines()
    events = {}
    for line in lines:
        crit, phase, ts = line.split()
        events.setdefault(crit, {})[phase] = float(ts)
    a_start, a_end = events["A001"]["start"], events["A001"]["end"]
    b_start, b_end = events["A002"]["start"], events["A002"]["end"]
    overlap = min(a_end, b_end) - max(a_start, b_start)
    assert overlap > 0, f"no timing overlap between the two criteria's agent runs: {events}"


def test_opted_in_chain_dependency_still_runs_in_order(tmp_path, monkeypatch):
    """A001 -> A002 declared via depends_on must NOT overlap even though
    criteria-level parallelism is opted in for this module."""
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    _write_module(pcp_dir, "add", [("A001", []), ("A002", ["A001"])])
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    timing_log = tmp_path / "timing.log"
    _fake_claude(tmp_path, monkeypatch, timing_log)

    with patch("pcp.commands.build._run_test_suite_check", return_value=[]), \
         patch("pcp.commands.build._run_lint_check", return_value=[]), \
         patch("pcp.commands.build._run_sast_check", return_value=[]), \
         patch("pcp.commands.build._run_layer1_check", return_value=[]), \
         patch("pcp.commands.build._run_architect_review", return_value=[]), \
         patch("pcp.commands.build._run_gate_check", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--module", "add", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "in parallel" not in result.output  # both waves are single-criterion

    lines = timing_log.read_text().splitlines()
    events = {}
    for line in lines:
        crit, phase, ts = line.split()
        events.setdefault(crit, {})[phase] = float(ts)
    assert events["A001"]["end"] <= events["A002"]["start"]
