import os
from pathlib import Path

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.check import (
    _read_bypass_reason, _run_ast_rule, _run_ast_required_rule, run_file_exists_rule,
    run_protected_path_rule, is_syntax_only_yaml_fix,
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


# ── ast_pattern with require_present: true — inverted, block-if-missing ──

def test_ast_required_rule_passes_when_pattern_present(tmp_path):
    (tmp_path / "agent.py").write_text("def send():\n    policy.strip(payload)\n")
    rule = {"id": "POLICY_001", "pattern": r"policy\.strip\(", "scope": ["agent.py"], "require_present": True}
    assert _run_ast_required_rule(rule, tmp_path) == []


def test_ast_required_rule_blocks_when_pattern_missing(tmp_path):
    (tmp_path / "agent.py").write_text("def send():\n    transmit(payload)\n")
    rule = {"id": "POLICY_001", "pattern": r"policy\.strip\(", "scope": ["agent.py"], "require_present": True}
    violations = _run_ast_required_rule(rule, tmp_path)
    assert len(violations) == 1
    assert "not found" in violations[0]


def test_ast_required_rule_blocks_when_scope_matches_no_files(tmp_path):
    rule = {"id": "POLICY_001", "pattern": r"policy\.strip\(", "scope": ["nonexistent.py"], "require_present": True}
    violations = _run_ast_required_rule(rule, tmp_path)
    assert len(violations) == 1
    assert "matched no files" in violations[0]


def test_ast_required_rule_requires_scope(tmp_path):
    rule = {"id": "POLICY_001", "pattern": r"x", "require_present": True}
    violations = _run_ast_required_rule(rule, tmp_path)
    assert len(violations) == 1
    assert "no scope" in violations[0]


def test_ast_required_rule_satisfied_by_any_file_in_scope(tmp_path):
    """Aggregate semantics: pattern must appear somewhere across scope, not in
    every scoped file individually."""
    (tmp_path / "a.py").write_text("no match here\n")
    (tmp_path / "b.py").write_text("policy.strip(x)\n")
    rule = {"id": "POLICY_001", "pattern": r"policy\.strip\(", "scope": ["*.py"], "require_present": True}
    assert _run_ast_required_rule(rule, tmp_path) == []


def test_ci_rules_schema_requires_scope_when_require_present_true():
    from pcp.schema.validator import validate_file

    p_bad = None
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p_bad = Path(d) / "ci_rules.yaml"
        p_bad.write_text(yaml.dump({"version": "1.0", "rules": [
            {"id": "POLICY_001", "name": "n", "check": "ast_pattern", "pattern": "x",
             "severity": "hard_block", "require_present": True},
        ]}))
        errors = validate_file(p_bad, "ci_rules")
        assert errors, "require_present: true with no scope should fail schema validation"

        p_good = Path(d) / "ci_rules_ok.yaml"
        p_good.write_text(yaml.dump({"version": "1.0", "rules": [
            {"id": "POLICY_001", "name": "n", "check": "ast_pattern", "pattern": "x",
             "severity": "hard_block", "require_present": True, "scope": ["agent.py"]},
        ]}))
        assert validate_file(p_good, "ci_rules") == []


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


# ── is_syntax_only_yaml_fix — deterministic carve-out for pure parse-error fixes ──

def test_syntax_only_fix_quoting_a_colon_containing_bullet_is_allowed():
    old = "criteria:\n  - id: A001\n    description: some text: with a colon\n"
    new = "criteria:\n  - id: A001\n    description: \"some text: with a colon\"\n"
    assert is_syntax_only_yaml_fix(old, new) is True


def test_syntax_only_fix_rejects_new_text_that_still_does_not_parse():
    old = "criteria:\n  - id: A001\n    description: broken: text\n"
    new = "criteria:\n  - id: A001\n    description: still: broken\n"
    assert is_syntax_only_yaml_fix(old, new) is False


def test_syntax_only_fix_rejects_actual_content_change():
    old = "criteria:\n  - id: A001\n    description: \"original text\"\n"
    new = "criteria:\n  - id: A001\n    description: \"a completely different requirement\"\n"
    assert is_syntax_only_yaml_fix(old, new) is False


def test_syntax_only_fix_rejects_brand_new_file():
    new = "criteria:\n  - id: A001\n    description: \"new criterion\"\n"
    assert is_syntax_only_yaml_fix(None, new) is False


def test_protected_path_rule_allows_verified_syntax_only_fix(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("criteria:\n  - id: A001\n    description: some text: with a colon\n")
    subprocess.run(["git", "add", "spec.yaml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    spec_path.write_text("criteria:\n  - id: A001\n    description: \"some text: with a colon\"\n")

    monkeypatch.setenv("PCP_AGENT_SESSION", "1")
    rule = {"id": "R003", "scope": ["spec.yaml"]}
    violations = run_protected_path_rule(rule, ["spec.yaml"], tmp_path)
    assert violations == []


def test_protected_path_rule_still_blocks_real_content_change(tmp_path, monkeypatch):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("criteria:\n  - id: A001\n    description: \"original\"\n")
    subprocess.run(["git", "add", "spec.yaml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    spec_path.write_text("criteria:\n  - id: A001\n    description: \"a totally different requirement\"\n")

    monkeypatch.setenv("PCP_AGENT_SESSION", "1")
    rule = {"id": "R003", "scope": ["spec.yaml"]}
    violations = run_protected_path_rule(rule, ["spec.yaml"], tmp_path)
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


def test_check_cli_blocks_on_missing_required_pattern(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "POLICY_001", "name": "strip before send", "check": "ast_pattern",
         "pattern": r"policy\.strip\(", "severity": "hard_block", "scope": ["agent.py"],
         "require_present": True},
    ])
    (tmp_path / "agent.py").write_text("def send():\n    transmit(payload)\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path), "--files", "agent.py"])
    assert result.exit_code == 1
    assert "POLICY_001" in result.output


def test_check_cli_passes_when_required_pattern_present(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "POLICY_001", "name": "strip before send", "check": "ast_pattern",
         "pattern": r"policy\.strip\(", "severity": "hard_block", "scope": ["agent.py"],
         "require_present": True},
    ])
    (tmp_path / "agent.py").write_text("def send():\n    policy.strip(payload)\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["check", "--path", str(tmp_path), "--files", "agent.py"])
    assert result.exit_code == 0


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
