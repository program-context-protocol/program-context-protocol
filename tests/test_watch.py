from unittest.mock import patch

from click.testing import CliRunner

from pcp.cli import cli


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
