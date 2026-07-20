"""ABC contract shape for ci_rules.yaml (CTRL-033 + _apply_rule_recovery),
2026-07-20 -- Agent Behavioral Contracts reference pattern (arXiv:2602.22302).
See docs/research-rigidity-vs-reliability-2026-07.md for the full trail."""

import yaml

from pcp import escalations
from pcp.commands.build import _apply_rule_recovery, _run_layer1_check, _run_wave_contract_completeness_check
from pcp.schema.validator import validate_file


def _ctx(module="widgets", criterion_id="A001", attempt=1):
    return {"module": module, "submodule": None, "criterion_id": criterion_id, "attempt": attempt, "files": []}


# ── schema accepts the optional contract block ──

def test_schema_accepts_rule_with_full_contract(tmp_path):
    path = tmp_path / "ci_rules.yaml"
    path.write_text(yaml.dump({
        "version": "1.0",
        "rules": [{
            "id": "R001", "name": "no secrets", "check": "ast_pattern",
            "pattern": "secret", "severity": "hard_block",
            "contract": {
                "preconditions": ["file is source code"],
                "invariants": ["never true after this rule was written"],
                "recovery": "escalate",
            },
        }],
    }))
    assert validate_file(path, "ci_rules") == []


def test_schema_rejects_invalid_recovery_value(tmp_path):
    path = tmp_path / "ci_rules.yaml"
    path.write_text(yaml.dump({
        "version": "1.0",
        "rules": [{
            "id": "R001", "name": "no secrets", "check": "ast_pattern",
            "pattern": "secret", "severity": "hard_block",
            "contract": {"recovery": "not-a-real-value"},
        }],
    }))
    assert validate_file(path, "ci_rules") != []


# ── _apply_rule_recovery ──

def test_apply_rule_recovery_noop_without_escalate(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rule = {"id": "R001", "contract": {"recovery": "retry"}}
    _apply_rule_recovery(pcp_dir, _ctx(), rule, "violation happened")
    assert escalations.load(pcp_dir) == []


def test_apply_rule_recovery_noop_without_contract_at_all(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rule = {"id": "R001"}
    _apply_rule_recovery(pcp_dir, _ctx(), rule, "violation happened")
    assert escalations.load(pcp_dir) == []


def test_apply_rule_recovery_escalates_immediately(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    rule = {"id": "R001", "contract": {"recovery": "escalate"}}
    _apply_rule_recovery(pcp_dir, _ctx(module="billing", criterion_id="A007"), rule, "secret leaked in app.py")
    entries = escalations.load(pcp_dir)
    assert len(entries) == 1
    assert entries[0]["module"] == "billing"
    assert entries[0]["criterion_id"] == "A007"
    assert "R001" in entries[0]["findings_preview"][0]


# ── _run_layer1_check fires the escalation end-to-end on a real violation ──

def test_layer1_check_escalates_on_contract_recovery_hit(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0",
        "rules": [{
            "id": "SEC_099", "name": "no TODO markers", "check": "ast_pattern",
            "pattern": "TODO", "severity": "hard_block",
            "contract": {"recovery": "escalate"},
        }],
    }))
    bad_file = project_root / "app.py"
    bad_file.write_text("# TODO fix this\n")

    violations = _run_layer1_check(pcp_dir, project_root, ["app.py"], _ctx())
    assert violations  # the rule actually fired
    entries = escalations.load(pcp_dir)
    assert len(entries) == 1
    assert "SEC_099" in entries[0]["findings_preview"][0]


def test_layer1_check_no_escalation_when_rule_has_no_contract(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0",
        "rules": [{
            "id": "SEC_100", "name": "no TODO markers", "check": "ast_pattern",
            "pattern": "TODO", "severity": "hard_block",
        }],
    }))
    (project_root / "app.py").write_text("# TODO fix this\n")

    violations = _run_layer1_check(pcp_dir, project_root, ["app.py"], _ctx())
    assert violations
    assert escalations.load(pcp_dir) == []


# ── CTRL-033 contract completeness (wave-level, advisory) ──

def test_contract_completeness_flags_hard_block_rule_with_no_contract(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0",
        "rules": [{"id": "R001", "name": "no secrets", "check": "ast_pattern", "pattern": "x", "severity": "hard_block"}],
    }))
    findings = _run_wave_contract_completeness_check(pcp_dir, 0)
    assert any("R001" in f for f in findings)


def test_contract_completeness_silent_when_contract_present(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0",
        "rules": [{
            "id": "R001", "name": "no secrets", "check": "ast_pattern", "pattern": "x",
            "severity": "hard_block", "contract": {"recovery": "block"},
        }],
    }))
    assert _run_wave_contract_completeness_check(pcp_dir, 0) == []


def test_contract_completeness_ignores_advisory_rules(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0",
        "rules": [{"id": "R001", "name": "style nit", "check": "ast_pattern", "pattern": "x", "severity": "advisory"}],
    }))
    assert _run_wave_contract_completeness_check(pcp_dir, 0) == []


def test_contract_completeness_inert_without_ci_rules_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert _run_wave_contract_completeness_check(pcp_dir, 0) == []
