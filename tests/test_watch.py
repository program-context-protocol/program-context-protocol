from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.watch import attempt_auto_fix


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def _run(tmp_path, extra_args=None, **patches):
    with patch("pcp.commands.watch.shutil.which", return_value="/usr/bin/gh"), \
            patch("pcp.commands.watch.time.sleep"), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.get_failed_logs", return_value="log output"), \
            patch("pcp.commands.watch.notify") as mock_notify, \
            patch("pcp.commands.watch.attempt_auto_fix", return_value=patches.pop("fix_succeeds", True)), \
            patch("pcp.commands.watch.get_latest_ci_run", side_effect=patches.pop("runs", [None])):
        runner = CliRunner()
        args = ["watch", "--path", str(tmp_path)] + (extra_args or [])
        result = runner.invoke(cli, args)
        return result, mock_notify


def test_nothing_to_watch_without_gh_or_health_url(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.watch.shutil.which", return_value=None), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.load_integrations", return_value={}):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "nothing to watch" in result.output


def test_once_flag_single_pass_success(tmp_path):
    _init_pcp(tmp_path)
    run = {"databaseId": 1, "status": "completed", "conclusion": "success", "name": "CI", "url": "http://x"}
    result, mock_notify = _run(tmp_path, extra_args=["--once"], runs=[run])
    assert result.exit_code == 0
    assert "CI run succeeded" in result.output
    mock_notify.assert_not_called()


def test_once_flag_failed_run_triggers_auto_fix(tmp_path):
    _init_pcp(tmp_path)
    run = {"databaseId": 1, "status": "completed", "conclusion": "failure", "name": "CI", "url": "http://x"}
    result, mock_notify = _run(tmp_path, extra_args=["--once"], runs=[run], fix_succeeds=True)
    assert result.exit_code == 0
    assert "CI run failed" in result.output
    assert "Attempting auto-fix" in result.output
    mock_notify.assert_called_once()
    assert "auto-fix attempted" in mock_notify.call_args[0][0]


def test_same_run_id_not_reprocessed(tmp_path):
    """A repeated poll seeing the same (already-handled) run shouldn't
    re-trigger auto-fix or re-print a result."""
    _init_pcp(tmp_path)
    run = {"databaseId": 1, "status": "completed", "conclusion": "success", "name": "CI", "url": "http://x"}
    result, mock_notify = _run(tmp_path, extra_args=["--max-iterations", "2"], runs=[run, run])
    assert result.exit_code == 0
    assert result.output.count("CI run succeeded") == 1


def test_max_iterations_stops_the_loop(tmp_path):
    _init_pcp(tmp_path)
    result, mock_notify = _run(tmp_path, extra_args=["--max-iterations", "3"], runs=[None, None, None])
    assert result.exit_code == 0
    assert "Reached max iterations (3)" in result.output


def test_consecutive_fix_breaker_pauses_auto_fix(tmp_path):
    """Regression-loop protection: after PCP_WATCH_MAX_CONSECUTIVE_FIXES (default 3)
    consecutive failed-CI polls each triggering an auto-fix attempt with no
    success in between, watch stops calling attempt_auto_fix and just reports."""
    _init_pcp(tmp_path)
    runs = [
        {"databaseId": i, "status": "completed", "conclusion": "failure", "name": "CI", "url": "http://x"}
        for i in range(1, 6)
    ]
    with patch("pcp.commands.watch.shutil.which", return_value="/usr/bin/gh"), \
            patch("pcp.commands.watch.time.sleep"), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.get_failed_logs", return_value="log"), \
            patch("pcp.commands.watch.notify") as mock_notify, \
            patch("pcp.commands.watch.attempt_auto_fix", return_value=True) as mock_fix, \
            patch("pcp.commands.watch.get_latest_ci_run", side_effect=runs):
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--path", str(tmp_path), "--max-iterations", "5"])

    assert result.exit_code == 0
    # 3 consecutive failures trigger auto-fix each time (attempts 1, 2, 3);
    # the breaker trips at attempt 3, so iterations 4 and 5 must NOT call it again.
    assert mock_fix.call_count == 3
    notify_messages = [c.args[0] for c in mock_notify.call_args_list]
    assert any("pausing auto-fix" in m for m in notify_messages)
    assert any("auto-fix paused, needs human attention" in m for m in notify_messages)


# ── attempt_auto_fix: budget cap + session reuse ──

def test_attempt_auto_fix_first_attempt_uses_session_id_flag(tmp_path):
    with patch("pcp.commands.watch.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        attempt_auto_fix(tmp_path / ".pcp", "log", "abc-123", True)
    cmd = mock_run.call_args[0][0]
    assert "--session-id" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "abc-123"
    assert "--resume" not in cmd


def test_attempt_auto_fix_subsequent_attempt_uses_resume_flag(tmp_path):
    with patch("pcp.commands.watch.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        attempt_auto_fix(tmp_path / ".pcp", "log", "abc-123", False)
    cmd = mock_run.call_args[0][0]
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in cmd


def test_attempt_auto_fix_includes_max_budget_usd_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_WATCH_AGENT_MAX_BUDGET_USD", "7")
    with patch("pcp.commands.watch.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        attempt_auto_fix(tmp_path / ".pcp", "log", "abc-123", True)
    cmd = mock_run.call_args[0][0]
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "7"


def test_consecutive_auto_fix_attempts_reuse_same_session_id(tmp_path):
    """Consecutive failed-CI polls within one streak should resume the same
    session, not cold-restart each time."""
    _init_pcp(tmp_path)
    runs = [
        {"databaseId": i, "status": "completed", "conclusion": "failure", "name": "CI", "url": "http://x"}
        for i in range(1, 3)
    ]
    with patch("pcp.commands.watch.shutil.which", return_value="/usr/bin/gh"), \
            patch("pcp.commands.watch.time.sleep"), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.get_failed_logs", return_value="log"), \
            patch("pcp.commands.watch.notify"), \
            patch("pcp.commands.watch.attempt_auto_fix", return_value=True) as mock_fix, \
            patch("pcp.commands.watch.get_latest_ci_run", side_effect=runs):
        runner = CliRunner()
        runner.invoke(cli, ["watch", "--path", str(tmp_path), "--max-iterations", "2"])

    assert mock_fix.call_count == 2
    first_session_id = mock_fix.call_args_list[0].args[2]
    second_session_id = mock_fix.call_args_list[1].args[2]
    assert first_session_id == second_session_id
    assert mock_fix.call_args_list[0].args[3] is True   # is_first_attempt
    assert mock_fix.call_args_list[1].args[3] is False  # resumed


def test_session_id_resets_to_fresh_after_ci_success(tmp_path):
    """A CI success must reset the session so the next real failure starts
    fresh, rather than resuming stale context from an unrelated failure."""
    _init_pcp(tmp_path)
    runs = [
        {"databaseId": 1, "status": "completed", "conclusion": "failure", "name": "CI", "url": "http://x"},
        {"databaseId": 2, "status": "completed", "conclusion": "success", "name": "CI", "url": "http://x"},
        {"databaseId": 3, "status": "completed", "conclusion": "failure", "name": "CI", "url": "http://x"},
    ]
    with patch("pcp.commands.watch.shutil.which", return_value="/usr/bin/gh"), \
            patch("pcp.commands.watch.time.sleep"), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.get_failed_logs", return_value="log"), \
            patch("pcp.commands.watch.notify"), \
            patch("pcp.commands.watch.attempt_auto_fix", return_value=True) as mock_fix, \
            patch("pcp.commands.watch.get_latest_ci_run", side_effect=runs):
        runner = CliRunner()
        runner.invoke(cli, ["watch", "--path", str(tmp_path), "--max-iterations", "3"])

    assert mock_fix.call_count == 2
    session_before_success = mock_fix.call_args_list[0].args[2]
    session_after_success = mock_fix.call_args_list[1].args[2]
    assert session_before_success != session_after_success
    assert mock_fix.call_args_list[1].args[3] is True  # fresh session again, not resumed


def test_deploy_health_check_failure_notifies(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    with patch("pcp.commands.watch.shutil.which", return_value=None), \
            patch("pcp.commands.watch.time.sleep"), \
            patch("pcp.commands.watch.check_environment"), \
            patch("pcp.commands.watch.load_integrations", return_value={"deploy": {"health_check_url": "http://x/health"}}), \
            patch("pcp.commands.watch.check_deploy_health", return_value=False), \
            patch("pcp.commands.watch.notify") as mock_notify:
        runner = CliRunner()
        result = runner.invoke(cli, ["watch", "--path", str(tmp_path), "--once"])
    assert result.exit_code == 0
    assert "health check FAILED" in result.output
    mock_notify.assert_called_once()
