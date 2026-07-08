import os
from pathlib import Path

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.check import (
    _read_bypass_reason, _run_ast_rule, run_file_exists_rule, run_protected_path_rule,
)


def _write_commit_msg(tmp_path, text):
    path = tmp_path / "commit_msg.txt"
    path.write_text(text)
    return path


# ── bypass-marker parsing — the exact bug scenarios found/fixed this project ──

def test_bypass_marker_recognized_on_its_own_line(tmp_path):
    msg = _write_commit_msg(tmp_path, "Fix the thing\n\n[pcp-bypass: known false positive]\n")
    assert _read_bypass_reason(msg) == "known false positive"


def test_bypass_marker_not_recognized_mid_sentence(tmp_path):
    """Real bug: prose mentioning the marker syntax mid-line must never trigger it."""
    msg = _write_commit_msg(
        tmp_path,
        "Fix bypass detection\n\n"
        "Scope the [pcp-bypass: reason] match to a full line so mentioning it\n"
        "in prose like this sentence never self-triggers.\n",
    )
    assert _read_bypass_reason(msg) is None


def test_bypass_marker_recognized_anywhere_in_multiline_unbroken_body(tmp_path):
    """Regression: an unbroken multi-line body (no blank line) with the marker
    alone on one of its lines must still match -- this is the real-usage case
    that a 'last paragraph only' heuristic previously missed."""
    msg = _write_commit_msg(
        tmp_path,
        "Ship a fix\n"
        "- did X\n"
        "[pcp-bypass: SEC_002 self-match on rule description text]\n"
        "- did Y\n",
    )
    assert _read_bypass_reason(msg) == "SEC_002 self-match on rule description text"


def test_bypass_marker_none_when_no_commit_msg_file():
    assert _read_bypass_reason(None) is None


def test_bypass_marker_none_when_file_missing(tmp_path):
    assert _read_bypass_reason(tmp_path / "nonexistent.txt") is None


def test_bypass_marker_ignores_comment_lines(tmp_path):
    msg = _write_commit_msg(tmp_path, "# [pcp-bypass: should not count, this is a comment]\n")
    assert _read_bypass_reason(msg) is None


# ── ast_pattern rule ──

def test_ast_rule_finds_violation_in_scoped_file(tmp_path):
    (tmp_path / "auth.py").write_text("password = 'hunter2'\n")
    rule = {"id": "SEC_001", "pattern": r"password\s*=\s*['\"]", "scope": ["*.py"]}
    violations = _run_ast_rule(rule, ["auth.py"], tmp_path)
    assert len(violations) == 1
    assert "auth.py:1" in violations[0]


def test_ast_rule_respects_scope_glob(tmp_path):
    (tmp_path / "auth.md").write_text("password = 'hunter2'\n")
    rule = {"id": "SEC_001", "pattern": r"password\s*=\s*['\"]", "scope": ["*.py"]}
    violations = _run_ast_rule(rule, ["auth.md"], tmp_path)
    assert violations == []


def test_ast_rule_skips_missing_files(tmp_path):
    rule = {"id": "SEC_001", "pattern": r"x", "scope": []}
    violations = _run_ast_rule(rule, ["does_not_exist.py"], tmp_path)
    assert violations == []


# ── file_exists rule, including {module} placeholder resolution ──

def test_file_exists_rule_literal_target_missing(tmp_path):
    rule = {"id": "MOD_001", "target": "README.md"}
    violations = run_file_exists_rule(rule, tmp_path, [])
    assert len(violations) == 1


def test_file_exists_rule_literal_target_present(tmp_path):
    (tmp_path / "README.md").touch()
    rule = {"id": "MOD_001", "target": "README.md"}
    assert run_file_exists_rule(rule, tmp_path, []) == []


def test_file_exists_rule_resolves_module_placeholder_per_module(tmp_path):
    (tmp_path / "src" / "modules").mkdir(parents=True)
    (tmp_path / "src" / "modules" / "add_interface.py").touch()
    rule = {"id": "MOD_003", "target": "src/modules/{module}_interface.py"}
    violations = run_file_exists_rule(rule, tmp_path, ["add", "subtract"])
    assert len(violations) == 1
    assert "subtract" in violations[0]


# ── protected_path rule — only fires inside a pcp-build agent session ──

def test_protected_path_rule_inert_outside_agent_session(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_AGENT_SESSION", raising=False)
    rule = {"id": "R003", "scope": [".pcp/objective.md"]}
    violations = run_protected_path_rule(rule, [".pcp/objective.md"])
    assert violations == []


def test_protected_path_rule_blocks_inside_agent_session(monkeypatch):
    monkeypatch.setenv("PCP_AGENT_SESSION", "1")
    rule = {"id": "R003", "scope": [".pcp/objective.md", ".pcp/strategy/**"]}
    violations = run_protected_path_rule(rule, [".pcp/objective.md"])
    assert len(violations) == 1
    assert "protected spec file" in violations[0]


# ── full CLI command ──

def _write_ci_rules(pcp_dir, rules):
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({"version": "1.0", "rules": rules}))


def test_check_cli_passes_with_no_violations(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "clean.py").write_text("x = 1\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path), "--files", "clean.py"])
    assert result.exit_code == 0
    assert "All rules passed" in result.output


def test_check_cli_blocks_on_hard_violation(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path), "--files", "bad.py"])
    assert result.exit_code == 1
    assert "BLOCKED" in result.output


def test_check_cli_advisory_violation_does_not_block(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "STYLE_001", "name": "Advisory style rule", "check": "ast_pattern",
         "pattern": r"TODO", "severity": "advisory", "scope": ["*.py"]},
    ])
    (tmp_path / "wip.py").write_text("# TODO: finish this\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path), "--files", "wip.py"])
    assert result.exit_code == 0
    assert "Advisory violations" in result.output


def test_check_cli_bypass_logs_and_exits_zero(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: known false positive, verified safe]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0
    assert "pcp-bypass" in result.output
    bypass_log = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())
    assert len(bypass_log["bypasses"]) == 1
    assert bypass_log["bypasses"][0]["reason"] == "known false positive, verified safe"
    assert bypass_log["bypasses"][0]["prev_hash"] == "genesis"
    assert "entry_hash" in bypass_log["bypasses"][0]


def test_bypass_log_entries_chain_across_multiple_bypasses(tmp_path):
    from pcp.evidence_chain import verify_chain
    from pcp.commands.check import _log_bypass

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _log_bypass(pcp_dir, "first bypass, reviewed by hand", ["SEC_001"])
    _log_bypass(pcp_dir, "second bypass, unrelated finding", ["SEC_002"])

    bypasses = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())["bypasses"]
    assert bypasses[1]["prev_hash"] == bypasses[0]["entry_hash"]
    assert verify_chain(bypasses) == []


def test_check_cli_no_ci_rules_skips(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "skipping check" in result.output


def test_check_cli_schema_error_blocks(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({"version": "1.0", "rules": [{"id": "bad-id-format"}]}))
    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "schema errors" in result.output


# ── bypass, backed by real OPA policy (.pcp/policies/bypass_approval.rego) ──

import shutil
import pytest

HAS_OPA = shutil.which("opa") is not None


def _write_bypass_policy(pcp_dir):
    policies_dir = pcp_dir / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    real = Path(".pcp") / "policies" / "bypass_approval.rego"
    (policies_dir / "bypass_approval.rego").write_text(real.read_text())


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_check_cli_opa_rejects_placeholder_bypass_reason(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    _write_bypass_policy(pcp_dir)
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: reason]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 1
    assert "rejected" in result.output
    assert not (pcp_dir / "bypass_log.yaml").exists()


@pytest.mark.skipif(not HAS_OPA, reason="opa binary not installed")
def test_check_cli_opa_approves_real_bypass_reason(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    _write_bypass_policy(pcp_dir)
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: known false positive, verified safe by security team]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0
    assert (pcp_dir / "bypass_log.yaml").exists()


def test_check_cli_bypass_permissive_without_opa_policy(tmp_path):
    """No .pcp/policies/ scaffolded (the common case) -- bypass stays
    permissive, exactly as before OPA was wired in."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: reason]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0
    assert (pcp_dir / "bypass_log.yaml").exists()
