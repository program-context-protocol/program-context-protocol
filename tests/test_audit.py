from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.audit import _run_vulture, _run_knip, _run_audit


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
