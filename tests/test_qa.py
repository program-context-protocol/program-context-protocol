from unittest.mock import patch, MagicMock

from pcp import qa


def test_run_test_suite_returns_no_tool_when_nothing_detected(tmp_path):
    with patch("shutil.which", return_value=None):
        result = qa.run_test_suite(tmp_path)
    assert result == {"tool": None, "passed": True, "output": ""}


def test_run_pytest_passed_on_zero_exit(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        result = qa.run_test_suite(tmp_path)
    assert result == {"tool": "pytest", "passed": True, "output": "1 passed"}


def test_run_pytest_no_tests_collected_counts_as_passed(tmp_path):
    """Exit code 5 = no tests collected yet -- not a failure."""
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=5, stdout="no tests ran", stderr="")
        result = qa.run_test_suite(tmp_path)
    assert result["passed"] is True


def test_run_pytest_failure(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="1 failed", stderr="")
        result = qa.run_test_suite(tmp_path)
    assert result["passed"] is False


def test_run_pytest_timeout_treated_as_failure(tmp_path):
    import subprocess as sp
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run", side_effect=sp.TimeoutExpired(cmd="pytest", timeout=300)):
        result = qa.run_test_suite(tmp_path)
    assert result["passed"] is False
    assert "timed out" in result["output"]


def test_run_lint_no_tool_detected(tmp_path):
    with patch("shutil.which", return_value=None):
        result = qa.run_lint(tmp_path, ["a.py"])
    assert result == {"tool": None, "passed": True, "issues": []}


def test_run_lint_ruff_skips_when_no_python_files_changed(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/ruff"):
        result = qa.run_lint(tmp_path, ["a.ts"])
    assert result == {"tool": "ruff", "passed": True, "issues": []}


def test_run_lint_ruff_reports_issues(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/ruff"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="a.py:1:1: F401 unused import\n", stderr="")
        result = qa.run_lint(tmp_path, ["a.py"])
    assert result["tool"] == "ruff"
    assert result["passed"] is False
    assert len(result["issues"]) == 1


def test_run_sast_skipped_when_no_semgrep(tmp_path):
    with patch("shutil.which", return_value=None):
        result = qa.run_sast(tmp_path, ["a.py"])
    assert result == {"tool": None, "passed": True, "findings": []}


def test_run_sast_skipped_when_no_changed_files(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/semgrep"):
        result = qa.run_sast(tmp_path, [])
    assert result["tool"] is None


def test_run_sast_finds_issues(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/semgrep"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="a.py:3 eval() detected\n", stderr="")
        result = qa.run_sast(tmp_path, ["a.py"])
    assert result["passed"] is False
    assert len(result["findings"]) == 1


def test_run_sast_never_raises_on_timeout(tmp_path):
    import subprocess as sp
    with patch("shutil.which", return_value="/usr/bin/semgrep"), \
            patch("pcp.qa.subprocess.run", side_effect=sp.TimeoutExpired(cmd="semgrep", timeout=120)):
        result = qa.run_sast(tmp_path, ["a.py"])
    assert result["passed"] is True
    assert result["skipped"] == "timed out"


def test_run_coverage_no_tool(tmp_path):
    with patch("shutil.which", return_value=None):
        result = qa.run_coverage(tmp_path)
    assert result == {"tool": None, "percent": None}
