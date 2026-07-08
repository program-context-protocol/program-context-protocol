from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.doctor import detect_tools, check_environment, _detect_one, _guess_deploy_command


def _fake_which(available: set):
    def which(name):
        return f"/usr/bin/{name}" if name in available else None
    return which


def test_detect_one_returns_first_match():
    with patch("shutil.which", side_effect=_fake_which({"npm"})):
        result = _detect_one(["pytest", "npm", "go"])
    assert result == {"tool": "npm", "available": True, "path": "/usr/bin/npm"}


def test_detect_one_none_available():
    with patch("shutil.which", side_effect=_fake_which(set())):
        result = _detect_one(["pytest", "npm"])
    assert result == {"tool": None, "available": False, "path": None}


def test_detect_tools_reports_all_categories():
    with patch("shutil.which", side_effect=_fake_which({"git", "claude"})):
        tools = detect_tools()
    assert tools["git"]["available"] is True
    assert tools["claude"]["available"] is True
    assert tools["gh"]["available"] is False
    assert tools["test_runner"]["available"] is False
    assert set(tools.keys()) == {
        "git", "claude", "gh", "test_runner", "lint", "sast", "coverage",
        "audit", "slack_notify", "opa", "temporal",
    }


def test_check_environment_fatal_on_missing_git(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which(set())):
        try:
            check_environment(pcp_dir, fatal_on_missing_required=True)
            assert False, "expected SystemExit"
        except SystemExit as e:
            assert e.code == 2


def test_check_environment_non_fatal_mode_returns_tools(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which(set())):
        tools = check_environment(pcp_dir, fatal_on_missing_required=False)
    assert tools["git"]["available"] is False


def test_check_environment_all_present_no_warnings(tmp_path, capsys):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    all_tools = {"git", "claude", "gh", "pytest", "ruff", "semgrep", "coverage",
                 "vulture", "slack-notify", "opa", "temporal"}
    with patch("shutil.which", side_effect=_fake_which(all_tools)):
        tools = check_environment(pcp_dir)
    assert tools["git"]["available"] is True


def test_guess_deploy_command_from_railway_toml(tmp_path):
    (tmp_path / "railway.toml").touch()
    assert _guess_deploy_command(tmp_path) == "railway up"


def test_guess_deploy_command_none_when_no_hint_files(tmp_path):
    assert _guess_deploy_command(tmp_path) is None


def test_doctor_cli_check_only_writes_nothing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which({"git", "claude"})):
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--path", str(tmp_path), "--check"])
    assert result.exit_code == 0
    assert not (pcp_dir / "integrations.yaml").exists()
    assert "PCP Environment Check" in result.output


def test_doctor_cli_interactive_writes_integrations(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which({"git", "claude"})):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--path", str(tmp_path)],
            input="railway up\nhttps://example.com/health\n\n",
        )
    assert result.exit_code == 0
    data = yaml.safe_load((pcp_dir / "integrations.yaml").read_text())
    assert data["deploy"]["command"] == "railway up"
    assert data["deploy"]["health_check_url"] == "https://example.com/health"
    assert data["deploy"]["rollback_command"] is None
