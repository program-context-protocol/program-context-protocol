import shutil
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.audit import (
    _run_vulture, _run_knip, _run_audit,
    _run_ast_grep_swallowed_exceptions, _run_jscpd, _run_coverage_check,
)

HAS_AST_GREP = shutil.which("ast-grep") is not None
HAS_JSCPD = shutil.which("jscpd") is not None


def test_run_vulture_absent_returns_none(tmp_path):
    with patch("shutil.which", return_value=None):
        assert _run_vulture(tmp_path) is None


def test_run_vulture_no_python_project_returns_none(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/vulture"):
        assert _run_vulture(tmp_path) is None


def test_run_vulture_finds_dead_code(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    with patch("shutil.which", return_value="/usr/bin/vulture"), \
            patch("pcp.commands.audit.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="src/foo.py:10: unused function 'bar'\n")
        result = _run_vulture(tmp_path)
    assert result["tool"] == "vulture"
    assert len(result["findings"]) == 1


def test_run_knip_absent_returns_none(tmp_path):
    assert _run_knip(tmp_path) is None


def test_run_audit_no_tools_returns_none_tool(tmp_path):
    with patch("shutil.which", return_value=None):
        result = _run_audit(tmp_path)
    assert result == {"tool": None, "findings": []}


def test_audit_cli_no_tool_detected(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", return_value=None):
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No dead-code tool detected" in result.output
    audit_md = (pcp_dir / "audit.md").read_text()
    assert "No audit tool detected" in audit_md


def test_audit_cli_reports_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "pyproject.toml").touch()
    with patch("shutil.which", return_value="/usr/bin/vulture"), \
            patch("pcp.commands.audit.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="a.py:1: unused import 'os'\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "1 dead-code finding(s)" in result.output
    audit_md = (pcp_dir / "audit.md").read_text()
    assert "unused import" in audit_md


def test_audit_cli_quiet_suppresses_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", return_value=None):
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--path", str(tmp_path), "--quiet"])
    assert result.exit_code == 0
    assert result.output == ""


# ── ast-grep: swallowed exceptions ──

def test_ast_grep_absent_returns_none(tmp_path):
    with patch("shutil.which", return_value=None):
        assert _run_ast_grep_swallowed_exceptions(tmp_path) is None


@pytest.mark.skipif(not HAS_AST_GREP, reason="ast-grep binary not installed")
def test_ast_grep_finds_real_swallowed_exception(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    result = _run_ast_grep_swallowed_exceptions(tmp_path)
    assert result["tool"] == "ast-grep"
    assert len(result["findings"]) == 1
    assert "bad.py" in result["findings"][0]


@pytest.mark.skipif(not HAS_AST_GREP, reason="ast-grep binary not installed")
def test_ast_grep_does_not_flag_real_handling(tmp_path):
    (tmp_path / "good.py").write_text(
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        logger.exception('failed')\n"
    )
    result = _run_ast_grep_swallowed_exceptions(tmp_path)
    assert result["findings"] == []


# ── jscpd: duplication ──

def test_jscpd_absent_returns_none(tmp_path):
    with patch("shutil.which", return_value=None):
        assert _run_jscpd(tmp_path) is None


@pytest.mark.skipif(not HAS_JSCPD, reason="jscpd binary not installed")
def test_jscpd_finds_real_duplication(tmp_path):
    # jscpd's default min-tokens threshold needs a real-sized block -- a
    # 7-line/36-token block (verified against jscpd 5.0.14 locally) sits
    # below the default and is silently not reported, so this uses a block
    # comfortably above it rather than tuning jscpd's own sensible defaults
    # down just to make a toy test pass.
    block = (
        "def process_order(order):\n"
        "    if order.status == 'pending':\n"
        "        order.validate()\n"
        "        order.charge()\n"
        "        order.notify()\n"
        "        order.log()\n"
        "        order.archive()\n"
        "        order.finalize()\n"
        "        order.send_receipt()\n"
        "        order.update_inventory()\n"
        "    return order\n"
    )
    (tmp_path / "a.py").write_text(block)
    (tmp_path / "b.py").write_text(block)
    result = _run_jscpd(tmp_path)
    assert result["tool"] == "jscpd"
    assert result["duplication_pct"] > 0
    assert len(result["findings"]) >= 1


@pytest.mark.skipif(not HAS_JSCPD, reason="jscpd binary not installed")
def test_jscpd_no_duplication_reports_zero(tmp_path):
    (tmp_path / "a.py").write_text("def unique_a():\n    return 1\n")
    (tmp_path / "b.py").write_text("def unique_b():\n    return 2\n")
    result = _run_jscpd(tmp_path)
    assert result["tool"] == "jscpd"
    assert result["duplication_pct"] == 0.0
    assert result["findings"] == []


# ── coverage: advisory-first threshold flip ──

def test_coverage_below_threshold_flagged(tmp_path):
    with patch("pcp.qa.run_coverage", return_value={"tool": "coverage", "percent": 30.0}):
        result = _run_coverage_check(tmp_path)
    assert result["below_threshold"] is True
    assert result["threshold"] == 50


def test_coverage_above_threshold_not_flagged(tmp_path):
    with patch("pcp.qa.run_coverage", return_value={"tool": "coverage", "percent": 85.0}):
        result = _run_coverage_check(tmp_path)
    assert result["below_threshold"] is False


def test_coverage_no_tool_never_flagged(tmp_path):
    with patch("pcp.qa.run_coverage", return_value={"tool": None, "percent": None}):
        result = _run_coverage_check(tmp_path)
    assert result["below_threshold"] is False


def test_coverage_threshold_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_COVERAGE_ADVISORY_THRESHOLD", "90")
    with patch("pcp.qa.run_coverage", return_value={"tool": "coverage", "percent": 85.0}):
        result = _run_coverage_check(tmp_path)
    assert result["threshold"] == 90
    assert result["below_threshold"] is True


def test_audit_cli_coverage_flag_off_by_default(tmp_path):
    """Real cost (full instrumented test-suite run) -- must never fire unless
    explicitly requested, same posture as `pcp scan --coverage`."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", return_value=None), \
            patch("pcp.qa.run_coverage") as mock_cov:
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    mock_cov.assert_not_called()


def test_audit_cli_coverage_flag_warns_below_threshold(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", return_value=None), \
            patch("pcp.qa.run_coverage", return_value={"tool": "coverage", "percent": 20.0}):
        runner = CliRunner()
        result = runner.invoke(cli, ["audit", "--path", str(tmp_path), "--coverage"])
    assert result.exit_code == 0
    assert "20% coverage" in result.output
    assert "below" in result.output.lower()
    audit_md = (pcp_dir / "audit.md").read_text()
    assert "Below advisory threshold" in audit_md
