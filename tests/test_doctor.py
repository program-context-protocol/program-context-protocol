import json
from unittest.mock import patch, MagicMock

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.doctor import (
    detect_tools, check_environment, _detect_one, _guess_deploy_command,
    detect_context7, configure_context7, check_schema_bloat, fix_schema_bloat,
)


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
        "audit", "slack_notify", "opa", "temporal", "npx", "agy",
    }


# ── PCP_CLAUDE_BIN respected by claude detection (2026-07-18 CI fix: this
# used to always check a literal `claude` on PATH, ignoring the env override
# llm/client.py's own _claude_bin() already respects -- silently agreed with
# reality on any dev machine with a real `claude` install, silently
# disagreed the moment an environment (GitHub Actions) has a working
# PCP_CLAUDE_BIN stub but no real `claude` binary on PATH at all) ──

def test_detect_tools_claude_ignores_bare_path_when_no_real_claude_installed(monkeypatch):
    monkeypatch.delenv("PCP_CLAUDE_BIN", raising=False)
    with patch("shutil.which", side_effect=_fake_which({"git"})):
        tools = detect_tools()
    assert tools["claude"]["available"] is False


def test_detect_tools_claude_respects_pcp_claude_bin_override(monkeypatch):
    monkeypatch.setenv("PCP_CLAUDE_BIN", "/tmp/fake_claude_stub.py")
    with patch("shutil.which", side_effect=_fake_which({"git", "/tmp/fake_claude_stub.py"})):
        tools = detect_tools()
    assert tools["claude"]["available"] is True


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


# ── Context7 detection/configuration ──

def test_detect_context7_no_npx_no_config(tmp_path):
    with patch("shutil.which", side_effect=_fake_which(set())):
        result = detect_context7(tmp_path)
    assert result["npx_available"] is False
    assert result["configured"] is False


def test_detect_context7_configured_when_mcp_json_has_it(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"context7": {"command": "npx"}}}))
    with patch("shutil.which", side_effect=_fake_which({"npx"})):
        result = detect_context7(tmp_path)
    assert result["npx_available"] is True
    assert result["configured"] is True


def test_detect_context7_not_configured_when_mcp_json_lacks_it(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other-server": {}}}))
    with patch("shutil.which", side_effect=_fake_which({"npx"})):
        result = detect_context7(tmp_path)
    assert result["configured"] is False


def test_detect_context7_invalid_json_treated_as_not_configured(tmp_path):
    (tmp_path / ".mcp.json").write_text("not valid json{{{")
    result = detect_context7(tmp_path)
    assert result["configured"] is False


def test_configure_context7_creates_mcp_json(tmp_path):
    ok = configure_context7(tmp_path)
    assert ok is True
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["context7"] == {"command": "npx", "args": ["-y", "@upstash/context7-mcp@latest"]}


def test_configure_context7_preserves_existing_servers(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other-server": {"command": "foo"}}}))
    configure_context7(tmp_path)
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert "other-server" in data["mcpServers"]
    assert "context7" in data["mcpServers"]


def test_configure_context7_refuses_to_clobber_invalid_json(tmp_path):
    (tmp_path / ".mcp.json").write_text("not valid json{{{")
    ok = configure_context7(tmp_path)
    assert ok is False
    assert (tmp_path / ".mcp.json").read_text() == "not valid json{{{"


def test_doctor_cli_offers_context7_when_npx_available(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which({"git", "claude", "npx"})):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["doctor", "--path", str(tmp_path)],
            input="\n\n\ny\n",  # deploy cmd, health url, rollback cmd (all blank), context7 confirm=yes
        )
    assert result.exit_code == 0, result.output
    assert "Context7" in result.output
    mcp_config = json.loads((tmp_path / ".mcp.json").read_text())
    assert "context7" in mcp_config["mcpServers"]
    integrations = yaml.safe_load((pcp_dir / "integrations.yaml").read_text())
    assert integrations["context7"]["configured"] is True


def test_doctor_cli_check_only_reports_context7_without_writing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("shutil.which", side_effect=_fake_which({"git", "claude", "npx"})):
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--path", str(tmp_path), "--check"])
    assert result.exit_code == 0
    assert "Context7" in result.output
    assert not (tmp_path / ".mcp.json").exists()


# ── Postgres schema-bloat preflight (2026-07-24, Project O incident) ──

def test_schema_bloat_none_without_postgres_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with patch("shutil.which", side_effect=_fake_which({"psql"})):
        assert check_schema_bloat() is None


def test_schema_bloat_none_without_psql(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which(set())):
        assert check_schema_bloat() is None


def test_schema_bloat_unsafe_pattern_returns_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"psql"})):
        assert check_schema_bloat(pattern="'; DROP TABLE x; --") is None


def test_schema_bloat_reports_count_and_bloated_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="120\n")
        result = check_schema_bloat(threshold=50, pattern="test_%")
    assert result == {"count": 120, "pattern": "test_%", "threshold": 50, "bloated": True}


def test_schema_bloat_under_threshold_not_flagged(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="3\n")
        result = check_schema_bloat(threshold=50, pattern="test_%")
    assert result["bloated"] is False


def test_fix_schema_bloat_unsafe_pattern_rejected():
    result = fix_schema_bloat("bad pattern; drop")
    assert result["dropped"] == []
    assert "unsafe pattern" in result["errors"][0]


def test_fix_schema_bloat_drops_listed_schemas(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="test_a\ntest_b\n"),  # list
            MagicMock(returncode=0, stdout=""),  # drop test_a
            MagicMock(returncode=0, stdout=""),  # drop test_b
        ]
        result = fix_schema_bloat("test_%")
    assert result["dropped"] == ["test_a", "test_b"]
    assert result["errors"] == []


def test_fix_schema_bloat_records_per_schema_errors(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="test_a\n"),  # list
            MagicMock(returncode=1, stderr="permission denied"),  # drop fails
        ]
        result = fix_schema_bloat("test_%")
    assert result["dropped"] == []
    assert "permission denied" in result["errors"][0]


def test_doctor_cli_fix_schema_bloat_prompts_and_drops(tmp_path, monkeypatch):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"git", "claude", "psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="99\n"),      # count
            MagicMock(returncode=0, stdout="test_a\n"),  # list
            MagicMock(returncode=0, stdout=""),          # drop test_a
        ]
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--path", str(tmp_path), "--fix-schema-bloat", "--yes"])
    assert result.exit_code == 0
    assert "Dropped 1 schema(s)" in result.output


def test_doctor_cli_check_warns_on_bloat(tmp_path, monkeypatch):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgres://x/y")
    with patch("shutil.which", side_effect=_fake_which({"git", "claude", "psql"})), \
            patch("pcp.commands.doctor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="500\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["doctor", "--path", str(tmp_path), "--check"])
    assert result.exit_code == 0
    assert "schema bloat" in result.output.lower()
