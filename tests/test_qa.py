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
    assert result.get("scoped_to") is None  # no pcp_dir/changed_files given -- nothing to scope to


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
    # The evidence file must name the limit that was hit, the knob that
    # changes it, and that this is not a test failure -- a bare "timed out"
    # sends whoever reads it next to debug tests that may be fine.
    assert "PCP_QA_TEST_TIMEOUT_SEC" in result["output"]
    assert "300s" in result["output"]
    assert "NOT a test failure" in result["output"]


def test_timeout_message_flags_untuned_default(monkeypatch):
    monkeypatch.delenv("PCP_QA_TEST_TIMEOUT_SEC", raising=False)
    assert "PCP default" in qa._timeout_message("pytest")
    monkeypatch.setenv("PCP_QA_TEST_TIMEOUT_SEC", "900")
    msg = qa._timeout_message("pytest")
    assert "900s" in msg and "PCP default" not in msg


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


def test_scoping_is_the_default_not_an_opt_in(monkeypatch):
    """It used to be opt-in behind PCP_QA_TEST_SELECTION=impact and defaulted
    OFF, while the code's own docstrings described scoped-per-criterion as the
    design. Documented and disabled: every attempt ran the whole suite."""
    monkeypatch.delenv("PCP_QA_FULL_SUITE", raising=False)
    assert qa.full_suite_forced() is False
    monkeypatch.setenv("PCP_QA_FULL_SUITE", "1")
    assert qa.full_suite_forced() is True
    assert not hasattr(qa, "test_selection_enabled"), "the old opt-in must be gone, not renamed"


def test_full_suite_escape_hatch_disables_scoping(tmp_path, monkeypatch):
    """PCP_QA_FULL_SUITE=1 restores the pre-2026-07-27 always-full behaviour."""
    monkeypatch.setenv("PCP_QA_FULL_SUITE", "1")
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths") as mock_scope:
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    mock_scope.assert_not_called()
    assert mock_run.call_args.args[0] == ["pytest", "-q"]


def test_run_test_suite_scopes_pytest_args_when_enabled_and_paths_found(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_QA_FULL_SUITE", raising=False)
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
    monkeypatch.delenv("PCP_QA_FULL_SUITE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths", return_value=None):
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        result = qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    assert mock_run.call_args.args[0] == ["pytest", "-q"]
    assert result.get("scoped_to") is None


def test_run_test_suite_falls_back_to_full_when_scoping_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_QA_FULL_SUITE", raising=False)
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
            patch("pcp.qa.subprocess.run") as mock_run, \
            patch("pcp.impact.blast_radius_test_paths", side_effect=RuntimeError("boom")):
        mock_run.return_value = MagicMock(returncode=0, stdout="5 passed", stderr="")
        result = qa.run_test_suite(tmp_path, pcp_dir=tmp_path / ".pcp", changed_files=["src/x.py"])
    assert mock_run.call_args.args[0] == ["pytest", "-q"]
    assert result.get("scoped_to") is None


# ── testmon: incremental test selection, detected not depended on (2026-07-27) ──

def test_testmon_detected_by_asking_pytest_not_by_importing(tmp_path):
    """The interpreter running PCP is frequently not the one running the
    project's tests — PCP is commonly installed globally while the project has
    its own venv. Importing testmon here would answer the wrong question."""
    import inspect
    src = inspect.getsource(qa.testmon_available)
    assert '"pytest", "--help"' in src or "'pytest', '--help'" in src
    assert "import testmon" not in src


def test_testmon_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_QA_NO_TESTMON", "1")
    assert qa.testmon_available(tmp_path) is False


def test_incremental_run_passes_testmon_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_QA_NO_TESTMON", raising=False)
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
         patch.object(qa, "testmon_available", return_value=True), \
         patch("pcp.qa.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="3 passed", stderr="")
        out = qa._run_pytest(tmp_path, ["tests/mod"], incremental=True)
    assert "--testmon" in run.call_args.args[0]
    assert out["incremental"] is True


def test_non_incremental_run_never_uses_testmon(tmp_path):
    """The wave-merge gate computes the real answer — an incremental runner is
    a cache, and the wave boundary is exactly where a cache must be distrusted."""
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
         patch.object(qa, "testmon_available", return_value=True), \
         patch("pcp.qa.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="900 passed", stderr="")
        out = qa._run_pytest(tmp_path, None, incremental=False)
    assert "--testmon" not in run.call_args.args[0]
    assert out["incremental"] is False


def test_wave_merge_path_stays_full_and_unscoped(tmp_path):
    """run_test_suite with no pcp_dir/changed_files must never scope or cache."""
    with patch("shutil.which", return_value="/usr/bin/pytest"), \
         patch.object(qa, "testmon_available", return_value=True), \
         patch("pcp.qa.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="1279 passed", stderr="")
        qa.run_test_suite(tmp_path)
    assert run.call_args.args[0] == ["pytest", "-q"]


def test_broken_testmon_db_falls_back_to_a_real_run(tmp_path):
    """A corrupt testmon database and "nothing was affected" look alike from
    outside. Never report a pass on zero tests from a cache PCP cannot verify."""
    calls = []

    def fake(args, **kw):
        calls.append(args)
        if "--testmon" in args:
            return MagicMock(returncode=3, stdout="INTERNALERROR", stderr="")
        return MagicMock(returncode=0, stdout="42 passed", stderr="")

    with patch("shutil.which", return_value="/usr/bin/pytest"), \
         patch.object(qa, "testmon_available", return_value=True), \
         patch("pcp.qa.subprocess.run", side_effect=fake):
        out = qa._run_pytest(tmp_path, ["tests/mod"], incremental=True)

    assert len(calls) == 2, "must re-run without testmon"
    assert "--testmon" not in calls[1]
    assert out["passed"] is True
    assert out["incremental"] is False
    assert "testmon_fallback" in out


def test_testmon_cache_is_never_auto_committed():
    """Written every build, differs per worktree, not a deliverable — the exact
    shape that broke wave merges twice today."""
    from pcp.commands.build import _AUTO_COMMIT_EXCLUDES
    assert ":!.testmondata" in _AUTO_COMMIT_EXCLUDES
