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
    assert result["tool"] == "pytest"
    assert result["passed"] is True
    assert result["output"] == "1 passed"
    assert result.get("scoped_to") is None  # PCP_QA_TEST_SELECTION not set -- full suite, unscoped


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
        mock_run.return_value = MagicMock(returncode=1, stdout="a.py:3 unsafe-dynamic-call detected\n", stderr="")
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


# ── env-overridable timeouts (2026-07-18, ontology-foundry dogfood finding:
# these were bare hardcoded constants -- a real project's suite legitimately
# taking longer than the default always got falsely marked "timed out") ──

def test_timeout_test_defaults_to_300():
    assert qa._timeout_test() == 300


def test_timeout_test_reads_env_override(monkeypatch):
    monkeypatch.setenv("PCP_QA_TEST_TIMEOUT_SEC", "900")
    assert qa._timeout_test() == 900


def test_timeout_lint_reads_env_override(monkeypatch):
    monkeypatch.setenv("PCP_QA_LINT_TIMEOUT_SEC", "30")
    assert qa._timeout_lint() == 30


def test_timeout_sast_reads_env_override(monkeypatch):
    monkeypatch.setenv("PCP_QA_SAST_TIMEOUT_SEC", "45")
    assert qa._timeout_sast() == 45


def test_run_pytest_passes_overridden_timeout_to_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_QA_TEST_TIMEOUT_SEC", "900")
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        qa.run_test_suite(tmp_path)
    assert mock_run.call_args.kwargs["timeout"] == 900


def test_test_selection_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("PCP_QA_TEST_SELECTION", raising=False)
    assert qa.test_selection_enabled() is False
    monkeypatch.setenv("PCP_QA_TEST_SELECTION", "impact")
    assert qa.test_selection_enabled() is True


def test_run_test_suite_ignores_selection_when_flag_unset(tmp_path, monkeypatch):
    """Default behavior is unchanged -- full suite, no scoping -- unless the
    flag is explicitly set. No silent behavior change for existing projects."""
    monkeypatch.delenv("PCP_QA_TEST_SELECTION", raising=False)
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths") as mock_scope:
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    mock_scope.assert_not_called()
    assert mock_run.call_args.args[0] == ["pytest", "-q"]


def test_run_test_suite_scopes_pytest_args_when_enabled_and_paths_found(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_QA_TEST_SELECTION", "impact")
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths", return_value=["tests/test_x.py"]):
        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")
        result = qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    assert mock_run.call_args.args[0] == ["pytest", "-q", "tests/test_x.py"]
    assert result["scoped_to"] == ["tests/test_x.py"]


def test_run_test_suite_falls_back_to_full_when_scoping_returns_none(tmp_path, monkeypatch):
    """impact.py returning None means it couldn't confidently narrow the
    scope -- must run the full suite, never silently run zero tests."""
    monkeypatch.setenv("PCP_QA_TEST_SELECTION", "impact")
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths", return_value=None):
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        result = qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    assert mock_run.call_args.args[0] == ["pytest", "-q"]
    assert result.get("scoped_to") is None


def test_run_test_suite_falls_back_to_full_when_scoping_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_QA_TEST_SELECTION", "impact")
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths", side_effect=RuntimeError("boom")):
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        result = qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    assert mock_run.call_args.args[0] == ["pytest", "-q"]
    assert result.get("scoped_to") is None
