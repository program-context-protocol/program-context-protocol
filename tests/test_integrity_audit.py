import json
from pathlib import Path

import yaml

from pcp import integrity_audit


def _write_module(pcp_dir, name, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": name, "criteria": criteria}))


def _append_telemetry(pcp_dir, record):
    path = pcp_dir / "telemetry.jsonl"
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")


def test_analyze_returns_empty_with_no_telemetry(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert integrity_audit.analyze(pcp_dir) == []


def test_fast_completion_flagged_for_high_tier_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "mod", [{"id": "C1", "logic_tier": 6, "status": "complete"}])
    _append_telemetry(pcp_dir, {
        "cycle": "build", "module": "mod", "criterion_id": "C1", "duration_ms": 5000,
    })
    findings = integrity_audit.analyze(pcp_dir)
    assert any("mod/C1" in f and "suspiciously fast" in f for f in findings)


def test_fast_completion_not_flagged_for_low_tier_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "mod", [{"id": "C1", "logic_tier": 1, "status": "complete"}])
    _append_telemetry(pcp_dir, {
        "cycle": "build", "module": "mod", "criterion_id": "C1", "duration_ms": 5000,
    })
    findings = integrity_audit.analyze(pcp_dir)
    assert findings == []


def test_fast_completion_not_flagged_when_slow_enough(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "mod", [{"id": "C1", "logic_tier": 6, "status": "complete"}])
    _append_telemetry(pcp_dir, {
        "cycle": "build", "module": "mod", "criterion_id": "C1", "duration_ms": 600_000,
    })
    findings = integrity_audit.analyze(pcp_dir)
    assert findings == []


def test_placeholder_concentration_flags_outlier_module(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    # "noisy" module: every placeholder-shaped check flagged.
    for i in range(4):
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "customization", "module": "noisy",
            "criterion_id": f"C{i}", "errors": ["placeholder"],
        })
    # "clean" module: never flagged.
    for i in range(4):
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "customization", "module": "clean",
            "criterion_id": f"C{i}", "errors": [],
        })
    findings = integrity_audit.analyze(pcp_dir)
    assert any("noisy" in f and "outlier concentration" in f for f in findings)
    assert not any("clean:" in f for f in findings)


def test_placeholder_concentration_silent_when_uniform_across_modules(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    for mod in ("a", "b"):
        for i in range(4):
            _append_telemetry(pcp_dir, {
                "cycle": "qa", "check": "customization", "module": mod,
                "criterion_id": f"C{i}", "errors": ["placeholder"] if i < 2 else [],
            })
    findings = integrity_audit.analyze(pcp_dir)
    assert findings == []


def test_recurring_findings_flagged_across_min_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    for i in range(3):
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "arch", "module": "mod", "criterion_id": f"C{i}",
            "result": "block", "errors": ["missing null check on the incoming payload before use"],
        })
    findings = integrity_audit.analyze(pcp_dir)
    assert any("recurring near-verbatim" in f for f in findings)


def test_recurring_findings_not_flagged_below_threshold(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    for i in range(2):
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "arch", "module": "mod", "criterion_id": f"C{i}",
            "result": "block", "errors": ["missing null check on the incoming payload before use"],
        })
    findings = integrity_audit.analyze(pcp_dir)
    assert findings == []


def test_uniform_evidence_flagged_when_content_matches(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    evidence_dir = pcp_dir / "evidence"
    evidence_dir.mkdir()
    for i in range(3):
        rel = f"evidence/dup_{i}.txt"
        (pcp_dir / rel).write_text("identical templated output")
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "gate", "module": "mod", "criterion_id": f"C{i}",
            "evidence_path": rel,
        })
    findings = integrity_audit.analyze(pcp_dir)
    assert any("identical evidence content" in f for f in findings)


def test_uniform_evidence_not_flagged_when_content_differs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "evidence").mkdir()
    for i in range(3):
        rel = f"evidence/dup_{i}.txt"
        (pcp_dir / rel).write_text(f"distinct output number {i}")
        _append_telemetry(pcp_dir, {
            "cycle": "qa", "check": "gate", "module": "mod", "criterion_id": f"C{i}",
            "evidence_path": rel,
        })
    findings = integrity_audit.analyze(pcp_dir)
    assert findings == []
