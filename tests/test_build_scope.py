"""Build-agent scope guard (CTRL-018) — over-reach allowlist check."""

import json

import yaml

from pcp.commands.build import _is_test_file, _run_scope_check, _scope_allowlist_violations


def _make_mod(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    mod_dir = pcp_dir / "strategy" / "modules" / "auth"
    mod_dir.mkdir(parents=True)
    acc = {
        "version": "2.0",
        "criteria": [
            {"id": "A1", "description": "login", "target": "src/auth/login.py", "status": "pending"},
            {"id": "A2", "description": "logout", "target": "src/auth/logout.py", "status": "complete"},
        ],
    }
    acc_path = mod_dir / "acceptance.yaml"
    acc_path.write_text(yaml.dump(acc))
    mod = {"name": "auth", "acc_path": acc_path, "pending_criteria": [acc["criteria"][0]]}
    return pcp_dir, mod, acc["criteria"][0]


def test_declared_targets_and_tests_are_in_scope(tmp_path):
    _, mod, c = _make_mod(tmp_path)
    changed = [
        "src/auth/login.py",            # this criterion's target
        "src/auth/logout.py",           # another criterion's target, same module
        "tests/test_login.py",          # test file
        ".pcp/strategy/modules/auth/acceptance.yaml",  # own module spec dir
        ".pcp/design_system.md",        # first-UI-screen carve-out
    ]
    assert _scope_allowlist_violations(mod, c, changed) == []


def test_unrelated_file_is_a_violation(tmp_path):
    _, mod, c = _make_mod(tmp_path)
    violations = _scope_allowlist_violations(mod, c, ["src/payments/charge.py", "src/auth/login.py"])
    assert violations == ["src/payments/charge.py"]


def test_other_modules_spec_dir_is_a_violation(tmp_path):
    _, mod, c = _make_mod(tmp_path)
    violations = _scope_allowlist_violations(mod, c, [".pcp/strategy/modules/payments/acceptance.yaml"])
    assert len(violations) == 1


def test_is_test_file_heuristics():
    assert _is_test_file("tests/test_x.py")
    assert _is_test_file("src/__tests__/x.test.ts")
    assert _is_test_file("src/foo.spec.js")
    assert _is_test_file("tests/conftest.py")
    assert not _is_test_file("src/auth/login.py")
    assert not _is_test_file("contest.py")


def _ctx():
    return {"module": "auth", "submodule": None, "criterion_id": "A1", "attempt": 1, "files": []}


def test_warn_mode_records_but_does_not_block(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_BUILD_SCOPE_MODE", raising=False)
    pcp_dir, mod, c = _make_mod(tmp_path)
    findings = _run_scope_check(pcp_dir, mod, c, ["src/payments/charge.py"], _ctx())
    assert findings == []  # advisory: nothing returned into block_findings
    records = [json.loads(l) for l in (pcp_dir / "telemetry.jsonl").read_text().splitlines()]
    scope_records = [r for r in records if r.get("check") == "build-scope"]
    assert len(scope_records) == 1
    assert scope_records[0]["control_id"] == "CTRL-018"
    assert scope_records[0]["error_count"] == 1


def test_block_mode_returns_finding(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_BUILD_SCOPE_MODE", "block")
    pcp_dir, mod, c = _make_mod(tmp_path)
    findings = _run_scope_check(pcp_dir, mod, c, ["src/payments/charge.py"], _ctx())
    assert len(findings) == 1
    assert "CTRL-018" in findings[0]
    assert "src/payments/charge.py" in findings[0]


def test_off_mode_records_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_BUILD_SCOPE_MODE", "off")
    pcp_dir, mod, c = _make_mod(tmp_path)
    findings = _run_scope_check(pcp_dir, mod, c, ["src/payments/charge.py"], _ctx())
    assert findings == []
    records = [json.loads(l) for l in (pcp_dir / "telemetry.jsonl").read_text().splitlines()]
    scope_records = [r for r in records if r.get("check") == "build-scope"]
    assert scope_records[0]["result"] == "skipped"


def test_clean_changeset_records_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("PCP_BUILD_SCOPE_MODE", raising=False)
    pcp_dir, mod, c = _make_mod(tmp_path)
    findings = _run_scope_check(pcp_dir, mod, c, ["src/auth/login.py"], _ctx())
    assert findings == []
    records = [json.loads(l) for l in (pcp_dir / "telemetry.jsonl").read_text().splitlines()]
    scope_records = [r for r in records if r.get("check") == "build-scope"]
    assert scope_records[0]["result"] == "pass"
    assert scope_records[0].get("evidence_path")
