from datetime import datetime, timezone, timedelta

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.deploy_check import _check_criterion, _check_current_state_freshness


def _fresh_timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_timestamp():
    return (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── _check_criterion ──

def test_check_criterion_file_exists_pass(tmp_path):
    (tmp_path / "README.md").touch()
    ok, detail = _check_criterion({"check": "file_exists", "target": "README.md"}, tmp_path)
    assert ok is True


def test_check_criterion_file_exists_fail(tmp_path):
    ok, detail = _check_criterion({"check": "file_exists", "target": "MISSING.md"}, tmp_path)
    assert ok is False


def test_check_criterion_ast_pattern_pass(tmp_path):
    (tmp_path / "main.py").write_text("def entrypoint(): pass\n")
    ok, detail = _check_criterion(
        {"check": "ast_pattern", "target": "main.py", "pattern": r"def entrypoint"}, tmp_path,
    )
    assert ok is True


def test_check_criterion_ast_pattern_fail_missing_file(tmp_path):
    ok, detail = _check_criterion(
        {"check": "ast_pattern", "target": "missing.py", "pattern": r"def x"}, tmp_path,
    )
    assert ok is False
    assert "not found" in detail


def test_check_criterion_manual_trusts_status():
    ok, detail = _check_criterion({"check": "manual", "status": "complete"}, None)
    assert ok is True
    ok, detail = _check_criterion({"check": "manual", "status": "pending"}, None)
    assert ok is False


# ── freshness ──

def test_freshness_missing_current_state(tmp_path):
    ok, detail = _check_current_state_freshness(tmp_path)
    assert ok is False
    assert "not found" in detail


def test_freshness_fresh_file(tmp_path):
    (tmp_path / "current_state.md").write_text(f"# Current State\nGenerated: {_fresh_timestamp()}\n")
    ok, detail = _check_current_state_freshness(tmp_path)
    assert ok is True


def test_freshness_stale_file(tmp_path):
    (tmp_path / "current_state.md").write_text(f"# Current State\nGenerated: {_stale_timestamp()}\n")
    ok, detail = _check_current_state_freshness(tmp_path)
    assert ok is False


def test_freshness_missing_timestamp(tmp_path):
    (tmp_path / "current_state.md").write_text("# Current State\nno timestamp here\n")
    ok, detail = _check_current_state_freshness(tmp_path)
    assert ok is False
    assert "no timestamp" in detail


# ── full CLI command ──

def _sdlc(phases, current_phase="alpha"):
    return {"version": "1.0", "current_phase": current_phase, "phases": phases}


def test_deploy_check_cli_passes_all_criteria_met(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text(f"# Current State\nGenerated: {_fresh_timestamp()}\n")
    (tmp_path / "README.md").touch()
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([
        {"name": "alpha", "exit_criteria": [
            {"id": "E001", "description": "README exists", "check": "file_exists", "target": "README.md", "status": "pending"},
        ]},
    ])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "exit criteria met" in result.output


def test_deploy_check_cli_blocks_on_unmet_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text(f"# Current State\nGenerated: {_fresh_timestamp()}\n")
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([
        {"name": "alpha", "exit_criteria": [
            {"id": "E001", "description": "README exists", "check": "file_exists", "target": "README.md", "status": "pending"},
        ]},
    ])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output


def test_deploy_check_cli_blocks_on_stale_current_state(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text(f"# Current State\nGenerated: {_stale_timestamp()}\n")
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([
        {"name": "alpha", "exit_criteria": []},
    ])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path)])
    assert result.exit_code == 1


def test_deploy_check_cli_skip_freshness_flag(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text(f"# Current State\nGenerated: {_stale_timestamp()}\n")
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([
        {"name": "alpha", "exit_criteria": []},
    ])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path), "--skip-freshness"])
    assert result.exit_code == 0


def test_deploy_check_cli_manual_complete_trusted_without_reverification(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text(f"# Current State\nGenerated: {_fresh_timestamp()}\n")
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([
        {"name": "alpha", "exit_criteria": [
            {"id": "E001", "description": "PM signoff", "check": "manual", "status": "complete"},
        ]},
    ])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path)])
    assert result.exit_code == 0


def test_deploy_check_cli_unknown_phase(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "SDLC_phase.yaml").write_text(yaml.dump(_sdlc([{"name": "alpha", "exit_criteria": []}])))

    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path), "--phase", "nonexistent"])
    assert result.exit_code == 2


def test_deploy_check_cli_no_sdlc_file_skips(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy-check", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "skipping deploy-check" in result.output
