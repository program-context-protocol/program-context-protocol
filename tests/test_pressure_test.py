"""MAC-Bench-style adversarial-pressure compliance check (pcp.pressure_test),
2026-07-20 -- see docs/research-rigidity-vs-reliability-2026-07.md."""

import stat
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from pcp import pressure_test
from pcp.cli import cli


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


# ── pure functions ──

def test_build_pressure_prompt_appends_frame_additively():
    base = "Do the thing."
    result = pressure_test.build_pressure_prompt(base)
    assert result.startswith(base)
    assert "URGENT" in result
    assert len(result) > len(base)


def test_record_and_load_roundtrip(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    report = {"criterion_id": "A001", "module": "widgets", "delta": 2, "widened": True}
    pressure_test.record(pcp_dir, report)
    entries = pressure_test.load(pcp_dir)
    assert len(entries) == 1
    assert entries[0]["criterion_id"] == "A001"
    assert entries[0]["widened"] is True


def test_load_empty_without_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert pressure_test.load(pcp_dir) == []


# ── end-to-end: fake agent behaves differently under pressure framing ──

FAKE_AGENT_PRESSURE_SENSITIVE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import sys, json, subprocess

    prompt = sys.stdin.read()
    under_pressure = "URGENT" in prompt

    if under_pressure:
        # simulates corner-cutting: leaves a lazy marker AND a stub body
        content = "def compute_total():\\n    # TODO: handle discounts later\\n    pass\\n"
    else:
        content = "def compute_total():\\n    return sum(line_items)\\n"

    with open("app.py", "w") as f:
        f.write(content)
    subprocess.run(["git", "add", "app.py"])
    subprocess.run(["git", "commit", "-m", "feat: compute_total"], capture_output=True)

    envelope = {
        "is_error": False, "result": "done", "session_id": "fake-session",
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.001,
        "duration_ms": 50,
    }
    print(json.dumps(envelope))
""")


@pytest.fixture
def pressure_project(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = pcp_dir / "strategy" / "modules" / "billing"
    mod_dir.mkdir(parents=True)
    spec = {"version": "2.0", "module": "billing", "description": "handles billing totals",
            "objective_coverage": ["x"], "dependencies": [], "constraints": []}
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    criterion = {"id": "A001", "description": "compute total", "check": "manual",
                 "status": "pending", "target": "app.py"}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "criteria": [criterion]}))
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)

    fake_agent = tmp_path / "fake_claude.py"
    fake_agent.write_text(FAKE_AGENT_PRESSURE_SENSITIVE)
    fake_agent.chmod(fake_agent.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PCP_CLAUDE_BIN", str(fake_agent))

    mod = {"name": "billing", "spec_path": mod_dir / "spec.yaml", "acc_path": mod_dir / "acceptance.yaml", "spec": spec}
    return repo, pcp_dir, mod, criterion


def test_pressure_test_detects_widening_gap(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    report = pressure_test.run_pressure_test(pcp_dir, repo, mod, criterion)

    assert report["widened"] is True
    assert report["delta"] > 0
    assert report["baseline"]["total_advisory"] == 0
    assert report["pressure"]["total_advisory"] > 0
    assert report["pressure"]["advisory_counts"]["lazy-marker"] > 0


def test_pressure_test_cleans_up_both_worktrees(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    pressure_test.run_pressure_test(pcp_dir, repo, mod, criterion)

    worktree_list = _git(["worktree", "list"], repo).stdout
    assert "pressuretest" not in worktree_list
    branches = _git(["branch"], repo).stdout
    assert "pressuretest" not in branches


def test_pressure_test_never_merges_agent_changes_into_main(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    pressure_test.run_pressure_test(pcp_dir, repo, mod, criterion)
    # Neither variant's app.py should have landed on main -- pressure-testing
    # is measurement only, never a real build.
    assert not (repo / "app.py").exists()


def test_pressure_test_logs_report(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    pressure_test.run_pressure_test(pcp_dir, repo, mod, criterion)

    entries = pressure_test.load(pcp_dir)
    assert len(entries) == 1
    assert entries[0]["criterion_id"] == "A001"
    assert entries[0]["module"] == "billing"


# ── CLI wiring ──

def test_pressure_test_cmd_reports_widening_gap(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    fake_report = {
        "criterion_id": "A001", "module": "billing", "delta": 3, "widened": True,
        "baseline": {"total_advisory": 0, "advisory_counts": {}},
        "pressure": {"total_advisory": 3, "advisory_counts": {"lazy-marker": 3}},
    }
    with patch("pcp.pressure_test.run_pressure_test", return_value=fake_report) as mock_run:
        runner = CliRunner()
        result = runner.invoke(cli, ["pressure-test", "billing", "A001", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "Compliance gap widened under pressure" in result.output
    mock_run.assert_called_once()
    _, kwargs = mock_run.call_args
    assert mock_run.call_args[0][3]["id"] == "A001"


def test_pressure_test_cmd_errors_on_unknown_criterion(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    runner = CliRunner()
    result = runner.invoke(cli, ["pressure-test", "billing", "NOPE", "--path", str(repo)])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_pressure_test_cmd_errors_on_unknown_module(pressure_project):
    repo, pcp_dir, mod, criterion = pressure_project
    runner = CliRunner()
    result = runner.invoke(cli, ["pressure-test", "nope", "A001", "--path", str(repo)])
    assert result.exit_code == 2
    assert "not found" in result.output
