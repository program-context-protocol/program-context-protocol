import subprocess
from unittest.mock import patch

import yaml

from pcp.commands.build import _run_install_only, _build_one_criterion, _build_module_worker, _BuildBudget
from pcp import telemetry
from pcp.evidence_chain import verify_chain


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def _pcp_dir(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def _mod(name="widgets", spec=None):
    return {"name": name, "spec": spec or {}}


def _approvals(pcp_dir):
    path = pcp_dir / "install_approvals.yaml"
    if not path.exists():
        return []
    return (yaml.safe_load(path.read_text()) or {}).get("approvals", [])


def test_declined_approval_falls_through(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    with patch("pcp.commands.build.click.confirm", return_value=False):
        ok, findings = _run_install_only(
            pcp_dir, tmp_path, _mod(), criterion={"id": "A001"},
            install_command="true", candidate_desc="some-pkg", yes=False,
            budget=_BuildBudget(max_sessions=10),
        )
    assert ok is False
    assert "declined" in findings[0]
    approvals = _approvals(pcp_dir)
    assert len(approvals) == 1
    assert approvals[0]["decision"] == "reject"
    assert approvals[0]["actor"] == "human"


def test_yes_flag_skips_prompt_installs_and_passes(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    with patch("pcp.commands.build.click.confirm") as mock_confirm:
        ok, findings = _run_install_only(
            pcp_dir, tmp_path, _mod(), criterion={"id": "A001"},
            install_command="true", candidate_desc="some-pkg", yes=True,
            budget=_BuildBudget(max_sessions=10),
        )
    mock_confirm.assert_not_called()
    assert ok is True
    assert findings == []
    approvals = _approvals(pcp_dir)
    assert approvals[0]["decision"] == "confirm"
    assert approvals[0]["actor"] == "yes-flag"
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "install-only"]
    assert records[-1]["control_id"] == "CTRL-034"
    assert records[-1]["result"] == "pass"


def test_install_command_failure_blocks(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    ok, findings = _run_install_only(
        pcp_dir, tmp_path, _mod(), criterion={"id": "A001"},
        install_command="exit 1", candidate_desc="some-pkg", yes=True,
        budget=_BuildBudget(max_sessions=10),
    )
    assert ok is False
    assert "install_command failed" in findings[0]
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "install-only"]
    assert records[-1]["result"] == "block"
    # Approval was still granted (human/yes-flag confirmed the match) --
    # it's the install itself that failed, a separate signal.
    assert _approvals(pcp_dir)[0]["decision"] == "confirm"


def test_approval_log_is_hash_chained(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    _run_install_only(pcp_dir, tmp_path, _mod(), criterion={"id": "A001"}, install_command="true", candidate_desc="x", yes=True, budget=_BuildBudget(max_sessions=10))
    _run_install_only(pcp_dir, tmp_path, _mod(), criterion={"id": "A002"}, install_command="true", candidate_desc="y", yes=True, budget=_BuildBudget(max_sessions=10))
    breaks = verify_chain(_approvals(pcp_dir))
    assert breaks == []


def test_criterion_level_fast_path_skips_agent_spawn(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    mod = _mod()
    c = {"id": "A001", "description": "x", "install_only": True, "install_command": "true"}
    budget = _BuildBudget(10)
    with patch("pcp.commands.build._claude_bin", side_effect=AssertionError("agent should not spawn")):
        success, findings = _build_one_criterion(pcp_dir, tmp_path, mod, c, None, False, budget, True)
    assert success is True
    assert findings == []


def test_module_level_fast_path_marks_all_criteria_complete(tmp_path):
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    mod_dir = pcp_dir / "strategy" / "modules" / "widgets"
    mod_dir.mkdir(parents=True)
    acc_path = mod_dir / "acceptance.yaml"
    criteria = [{"id": "A001", "status": "pending"}, {"id": "A002", "status": "pending"}]
    acc_path.write_text(yaml.dump({"criteria": criteria}))
    mod = {
        "name": "widgets", "acc_path": acc_path,
        "spec": {"install_only": True, "install_command": "true"},
        "pending_criteria": criteria,
    }
    budget = _BuildBudget(10)
    with patch("pcp.commands.build._claude_bin", side_effect=AssertionError("agent should not spawn")):
        result = _build_module_worker(pcp_dir, mod, tmp_path, None, False, budget, True)
    assert result["success"] is True
    saved = yaml.safe_load(acc_path.read_text())
    assert all(c["status"] == "complete" for c in saved["criteria"])


def test_missing_install_command_does_not_crash_run_install_only_guard(tmp_path):
    """The install_only guard in _build_one_criterion checks for
    install_command before calling _run_install_only at all -- verify that
    guard directly rather than letting the test fall into a real agent spawn."""
    _git_repo(tmp_path)
    pcp_dir = _pcp_dir(tmp_path)
    mod = _mod()
    c = {"id": "A001", "description": "x", "install_only": True}  # no install_command
    with patch("pcp.commands.build._run_install_only") as mock_run:
        with patch("pcp.commands.build._claude_bin", side_effect=AssertionError("agent should not spawn in this test")):
            try:
                _build_one_criterion(pcp_dir, tmp_path, mod, c, None, False, _BuildBudget(10), True)
            except AssertionError:
                pass  # expected -- falls through to the (mocked-out) real agent path
    mock_run.assert_not_called()
