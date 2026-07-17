import yaml
from unittest.mock import patch

from pcp.commands.build import _run_wave_tier_drift_check
from pcp import telemetry


def _write_module(pcp_dir, project_root, name, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump({"dependencies": []}))
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": criteria}))
    return mod_dir


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def test_llm_sdk_import_under_low_tier_flags_drift(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "summarize.py").write_text("import anthropic\n\ndef f():\n    pass\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete", "logic_tier": 3, "target": "src/summarize.py"},
    ])

    findings = _run_wave_tier_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)

    assert len(findings) == 1
    assert "A001" in findings[0]
    assert "anthropic" in findings[0]
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "wave-tier-drift"][0]
    assert record["control_id"] == "CTRL-014"
    assert record["result"] == "block"


def test_rung_6_criterion_with_llm_import_is_not_flagged(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "judge.py").write_text("import anthropic\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete", "logic_tier": 6, "target": "src/judge.py"},
    ])

    findings = _run_wave_tier_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []


def test_pending_criterion_is_not_checked(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "wip.py").write_text("import anthropic\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "pending", "logic_tier": 3, "target": "src/wip.py"},
    ])

    findings = _run_wave_tier_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []


def test_no_llm_import_no_drift(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    (project_root / "src").mkdir()
    (project_root / "src" / "clean.py").write_text("def f():\n    return 1\n")
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete", "logic_tier": 1, "target": "src/clean.py"},
    ])

    findings = _run_wave_tier_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "wave-tier-drift"][0]
    assert record["result"] == "pass"


def test_missing_target_or_tier_is_skipped_not_errored(tmp_path):
    project_root = tmp_path
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, project_root, "widgets", [
        {"id": "A001", "description": "x", "status": "complete"},  # no logic_tier/target at all
    ])

    findings = _run_wave_tier_drift_check(pcp_dir, [{"name": "widgets"}], wave_number=0)
    assert findings == []
