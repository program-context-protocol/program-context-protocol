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
    assert _read_bypass_reason(msg) == ("known false positive", None)


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
    assert _read_bypass_reason(msg) == ("SEC_002 self-match on rule description text", None)


def test_bypass_marker_none_when_no_commit_msg_file():
    assert _read_bypass_reason(None) is None


def test_bypass_marker_none_when_file_missing(tmp_path):
    assert _read_bypass_reason(tmp_path / "nonexistent.txt") is None


def test_bypass_marker_ignores_comment_lines(tmp_path):
    msg = _write_commit_msg(tmp_path, "# [pcp-bypass: should not count, this is a comment]\n")
    assert _read_bypass_reason(msg) is None


# ── scoped bypass — real incident, 2026-07-30 ──────────────────────────────
#
# An ast_pattern rule (R008) matched its own text inside PCP's generated
# telemetry.jsonl -- a false positive against a file that was never supposed
# to be scanned. Bypass was all-or-nothing, so the one genuine false positive
# silently disabled R001 through R010 together for that commit. Scoping means
# `[pcp-bypass: R008: reason]` skips only R008 and everything else still runs.

def test_scoped_bypass_returns_the_rule_id_and_reason_separately(tmp_path):
    msg = _write_commit_msg(
        tmp_path, "Fix thing\n\n[pcp-bypass: R008: matched its own rule text in telemetry.jsonl]\n",
    )
    assert _read_bypass_reason(msg) == (
        "matched its own rule text in telemetry.jsonl", ["R008"],
    )


def test_scoped_bypass_accepts_multiple_comma_separated_ids(tmp_path):
    msg = _write_commit_msg(tmp_path, "[pcp-bypass: R003,R008: both false positives on generated files]\n")
    reason, ids = _read_bypass_reason(msg)
    assert ids == ["R003", "R008"]
    assert reason == "both false positives on generated files"


def test_scoped_bypass_recognizes_the_projects_real_id_convention(tmp_path):
    """Rule IDs aren't always R\\d+ -- ci_rules.schema.json allows any
    [A-Z]+_?[0-9]+, and real projects use SEC_/MOD_ prefixes."""
    msg = _write_commit_msg(tmp_path, "[pcp-bypass: SEC_002: known false positive on this file]\n")
    assert _read_bypass_reason(msg) == ("known false positive on this file", ["SEC_002"])


def test_unscoped_bypass_still_works_exactly_as_before(tmp_path):
    """No colon-prefixed rule id -- the original, still-default behaviour."""
    msg = _write_commit_msg(tmp_path, "[pcp-bypass: this is a normal blanket bypass]\n")
    assert _read_bypass_reason(msg) == ("this is a normal blanket bypass", None)


def test_a_reason_that_merely_contains_a_colon_is_not_mistaken_for_scoping(tmp_path):
    """'note: something' must not be parsed as rule-id 'note'."""
    msg = _write_commit_msg(tmp_path, "[pcp-bypass: note: this legacy path is intentionally unchecked]\n")
    reason, ids = _read_bypass_reason(msg)
    assert ids is None
    assert reason == "note: this legacy path is intentionally unchecked"


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


def test_protected_path_rule_blocks_inside_agent_session(monkeypatch, tmp_path):
    monkeypatch.setenv("PCP_AGENT_SESSION", "1")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    obj_path = pcp_dir / "objective.md"
    obj_path.write_text("original content\n")
    obj_path.write_text("agent edited this directly, never went through a gated command\n")
    rule = {"id": "R003", "scope": [".pcp/objective.md", ".pcp/strategy/**"]}
    violations = run_protected_path_rule(rule, [".pcp/objective.md"], tmp_path, pcp_dir)
    assert len(violations) == 1
    assert "protected spec file" in violations[0]


def test_protected_path_rule_allows_approved_write_inside_agent_session(monkeypatch, tmp_path):
    """An agent-session write that WAS stamped by a sanctioned command (e.g.
    pcp amend called with --yes from inside pcp build) is allowed through --
    the block targets unapproved direct edits, not the gated commands
    themselves."""
    monkeypatch.setenv("PCP_AGENT_SESSION", "1")
    from pcp import protected_writes
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    obj_path = pcp_dir / "objective.md"
    obj_path.write_text("original content\n")
    new_content = "sanctioned rewrite via pcp amend\n"
    obj_path.write_text(new_content)
    protected_writes.record_approved_write(pcp_dir, obj_path, new_content)
    rule = {"id": "R003", "scope": [".pcp/objective.md", ".pcp/strategy/**"]}
    violations = run_protected_path_rule(rule, [".pcp/objective.md"], tmp_path, pcp_dir)
    assert violations == []


def test_protected_path_rule_blocks_outside_agent_session_after_grandfather(monkeypatch, tmp_path):
    # Explicit, not ambient: build.py's own real build() sets this env var
    # directly (not via monkeypatch) for the CLI process's lifetime, which
    # can leak across tests sharing one pytest process if a build test runs
    # first. This test's whole point is the outside-agent-session path, so
    # it must not depend on nothing else having touched this var.
    monkeypatch.delenv("PCP_AGENT_SESSION", raising=False)
    """Outside a pcp-build agent session, a path with no prior approval
    record gets a one-time grandfather pass so an EXISTING project upgrading
    to this mechanism doesn't have its very next ordinary commit blocked by
    history that predates it -- but the following unapproved edit to the
    same path still blocks. Closes a real gap: the rule previously only
    fired inside pcp build's own agent session, so any other agent (or a
    human editing directly) could rewrite a protected file with zero
    warning -- confirmed by an independent cold-clone review, 2026-08-12."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    obj_path = pcp_dir / "objective.md"
    obj_path.write_text("original content\n")
    rule = {"id": "R003", "scope": [".pcp/objective.md", ".pcp/strategy/**"]}
    # First check ever for this path -- no record exists yet, grandfathered.
    violations = run_protected_path_rule(rule, [".pcp/objective.md"], tmp_path, pcp_dir)
    assert violations == []
    # A second, different unapproved edit right after -- now must block.
    obj_path.write_text("unapproved direct edit\n")
    violations = run_protected_path_rule(rule, [".pcp/objective.md"], tmp_path, pcp_dir)
    assert len(violations) == 1


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


def test_check_cli_scoped_bypass_skips_only_the_named_rule_others_still_block(tmp_path):
    """The actual incident: two rules fire, one is a false positive. Scoped
    bypass must skip only that one and still block on the real violation --
    proving the fix is not just parse-level but changes runtime behaviour."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "R008", "name": "false-positive-prone rule", "check": "ast_pattern",
         "pattern": r"FALSE_POSITIVE_MARKER", "severity": "hard_block", "scope": ["*.py"]},
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("FALSE_POSITIVE_MARKER\npassword = 'hunter2'\n")
    msg_file = _write_commit_msg(
        tmp_path, "Fix\n\n[pcp-bypass: R008: matched its own generated text, not a real finding]\n",
    )

    result = CliRunner().invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code != 0             # SEC_001 still blocks -- not silently skipped
    assert "SEC_001" in result.output or "No hardcoded secrets" in result.output

    bypass_log = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())
    assert bypass_log["bypasses"][0]["rules_bypassed"] == ["R008"]   # not SEC_001 too


def test_check_cli_scoped_bypass_exits_clean_when_the_only_violation_is_bypassed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "R008", "name": "false-positive-prone rule", "check": "ast_pattern",
         "pattern": r"FALSE_POSITIVE_MARKER", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("FALSE_POSITIVE_MARKER\n")
    msg_file = _write_commit_msg(tmp_path, "[pcp-bypass: R008: known false positive]\n")

    result = CliRunner().invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0


def test_check_cli_scoped_bypass_warns_on_an_unknown_rule_id_but_still_runs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "[pcp-bypass: R999: typo'd rule id]\n")

    result = CliRunner().invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "bad.py", "--commit-msg-file", str(msg_file),
    ])
    assert "R999" in result.output and "not found" in result.output
    assert result.exit_code != 0     # SEC_001 was never actually bypassed


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


# ── bypass module attribution — check.py:_attributed_modules ──────────────

def _write_module(pcp_dir, name, criteria=None):
    module_dir = pcp_dir / "strategy" / "modules" / name
    module_dir.mkdir(parents=True)
    (module_dir / "spec.yaml").write_text(yaml.dump({"description": f"{name} module"}))
    (module_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": criteria or []}))
    return module_dir


def test_check_cli_bypass_attributes_module_via_spec_dir_path(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    _write_module(pcp_dir, "auth")
    (tmp_path / "bad.py").write_text("password = 'hunter2'\n")
    rel_spec = str((pcp_dir / "strategy" / "modules" / "auth" / "spec.yaml").relative_to(tmp_path))
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: known false positive, verified safe]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", f"bad.py,{rel_spec}", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0
    bypass_log = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())
    assert bypass_log["bypasses"][0]["modules"] == ["auth"]


def test_check_cli_bypass_attributes_module_via_criterion_target(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_ci_rules(pcp_dir, [
        {"id": "SEC_001", "name": "No hardcoded secrets", "check": "ast_pattern",
         "pattern": r"password\s*=\s*['\"]", "severity": "hard_block", "scope": ["*.py"]},
    ])
    _write_module(pcp_dir, "billing", criteria=[{"id": "C1", "description": "x", "target": "src/billing.py"}])
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "billing.py").write_text("password = 'hunter2'\n")
    msg_file = _write_commit_msg(tmp_path, "Fix\n\n[pcp-bypass: known false positive, verified safe]\n")

    runner = CliRunner()
    result = runner.invoke(cli, [
        "check", "--path", str(tmp_path), "--files", "src/billing.py", "--commit-msg-file", str(msg_file),
    ])
    assert result.exit_code == 0
    bypass_log = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())
    assert bypass_log["bypasses"][0]["modules"] == ["billing"]


def test_check_cli_bypass_no_module_match_leaves_modules_empty(tmp_path):
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
    bypass_log = yaml.safe_load((pcp_dir / "bypass_log.yaml").read_text())
    assert bypass_log["bypasses"][0]["modules"] == []
    assert bypass_log["bypasses"][0]["files"] == ["bad.py"]


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
    # Sourced from the shipped template, not a local `.pcp/policies/` on
    # disk -- that dir is gitignored/maintainer-local, so a fresh clone has
    # none. Real incident, cold-clone review 2026-08-12.
    from pcp.commands.init import POLICY_BYPASS_TEMPLATE
    policies_dir = pcp_dir / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "bypass_approval.rego").write_text(POLICY_BYPASS_TEMPLATE)


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
