"""CTRL-039 (exposure-mode justification) and CTRL-040 (cross-module
user-flow wiring), added 2026-08-04. See build.py's _run_wave_exposure_check
/ _run_wave_flow_wiring_check and flow_wiring.py."""

from unittest.mock import patch

import yaml

from pcp import flow_wiring, telemetry
from pcp.commands.build import _run_wave_exposure_check, _run_wave_flow_wiring_check
from pcp.commands.design_audit import build_design_audit


def _mod(pcp_dir, name="widgets", criteria=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"criteria": criteria or []}))
    return {"name": name}


def _wave_records(pcp_dir, check):
    return [r for r in telemetry.load(pcp_dir) if r.get("check") == f"wave-{check}"]


# ── CTRL-039: exposure-mode justification ──

def test_exposure_absent_field_not_flagged(tmp_path):
    """Default is 'ui' -- the pre-existing behavior every UI criterion
    already had before this field existed. Absence is not a finding."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{"id": "A001", "description": "Dashboard renders coverage", "status": "complete"}])
    findings = _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0)
    assert findings == []


def test_exposure_ui_mode_not_flagged_even_with_no_justification(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
        "exposure": {"mode": "ui"},
    }])
    assert _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_exposure_flags_empty_justification(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
        "exposure": {"mode": "api", "justification": ""},
    }])
    findings = _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    assert "(empty)" in findings[0]


def test_exposure_flags_placeholder_justification(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
        "exposure": {"mode": "internal", "justification": "TODO"},
    }])
    findings = _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1


def test_exposure_passes_with_real_justification(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
        "exposure": {"mode": "api", "justification": "Exposed via POST /api/export, no UI surface this phase."},
    }])
    assert _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_exposure_skips_non_ui_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Computes correct percentage", "status": "complete",
        "exposure": {"mode": "internal", "justification": ""},
    }])
    assert _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_exposure_skips_incomplete_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "pending",
        "exposure": {"mode": "api", "justification": ""},
    }])
    assert _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_exposure_records_advisory_telemetry_not_literal_pass(tmp_path):
    """A finding must never be recorded as a literal 'pass' -- CTRL-008/019/
    020/021/025/027/028/030/031/033/036 all learned this lesson already
    (see _wave_record's own docstring); CTRL-039 reuses the same mechanic."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{
        "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
        "exposure": {"mode": "api", "justification": "x"},
    }])
    _run_wave_exposure_check(pcp_dir, [{"name": "widgets"}], 0)
    record = _wave_records(pcp_dir, "exposure-justification")[0]
    assert record["control_id"] == "CTRL-039"
    assert record["result"] == "advisory"


# ── design_audit: non-ui-exposed criteria excluded from the ladder ──

def test_design_audit_excludes_non_ui_exposure_from_ladder(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {
            "id": "A001", "description": "Dashboard renders coverage", "status": "complete",
            "exposure": {"mode": "api", "justification": "Exposed via POST /api/export."},
        },
    ])
    data = build_design_audit(pcp_dir)
    assert data["total_ui_criteria"] == 0
    assert sum(data["rung_counts"].values()) == 0
    assert data["undetermined"] == 0
    assert len(data["non_ui_exposed"]) == 1
    assert data["non_ui_exposed"][0]["mode"] == "api"


def test_design_audit_ui_mode_still_counted_normally(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A001", "description": "Dashboard renders coverage", "status": "complete", "exposure": {"mode": "ui"}},
    ])
    data = build_design_audit(pcp_dir)
    assert data["total_ui_criteria"] == 1
    assert data["non_ui_exposed"] == []


# ── flow_wiring: load/parse/runnability ──

def test_load_flows_missing_file_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert flow_wiring.load_flows(pcp_dir) == []


def test_load_flows_parses_real_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy").mkdir(parents=True)
    flows = [{"id": "f1", "modules_spanned": ["a", "b"], "steps": [{"action": "navigate", "target": "/"}]}]
    (pcp_dir / "strategy" / "user_flows.yaml").write_text(yaml.dump({"flows": flows}))
    loaded = flow_wiring.load_flows(pcp_dir)
    assert loaded == flows


def test_load_flows_unparseable_returns_empty_not_raises(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy").mkdir(parents=True)
    (pcp_dir / "strategy" / "user_flows.yaml").write_text("flows: [1, 2\nbroken: [")
    assert flow_wiring.load_flows(pcp_dir) == []


def test_flow_is_runnable_true_when_spanned_modules_complete_and_in_wave():
    flow = {"modules_spanned": ["a", "b"]}
    assert flow_wiring.flow_is_runnable(flow, [{"name": "b"}], {"a", "b"}) is True


def test_flow_is_runnable_false_when_a_spanned_module_incomplete():
    flow = {"modules_spanned": ["a", "b"]}
    assert flow_wiring.flow_is_runnable(flow, [{"name": "b"}], {"b"}) is False


def test_flow_is_runnable_false_when_no_spanned_module_in_this_wave(tmp_path):
    """All spanned modules complete, but none of them finished in THIS wave --
    already satisfied earlier, must not re-run/re-record every wave forever."""
    flow = {"modules_spanned": ["a", "b"]}
    assert flow_wiring.flow_is_runnable(flow, [{"name": "c"}], {"a", "b", "c"}) is False


def test_flow_is_runnable_false_when_no_modules_spanned_declared():
    assert flow_wiring.flow_is_runnable({"modules_spanned": []}, [{"name": "a"}], {"a"}) is False


# ── flow_wiring.run_flow: could-not-check posture ──

def test_run_flow_no_base_url_fails_not_skips():
    ok, detail = flow_wiring.run_flow({"id": "f1", "steps": [{"action": "navigate", "target": "/"}]}, None)
    assert ok is False
    assert "base_url" in detail


def test_run_flow_no_steps_fails():
    ok, detail = flow_wiring.run_flow({"id": "f1", "steps": []}, "http://localhost:3000")
    assert ok is False
    assert "no steps" in detail


def test_run_flow_playwright_missing_returns_none_not_false():
    """Same could-not-check-vs-failed posture as uat.check_visual -- a
    missing optional dependency must never read as a broken wiring."""
    flow = {"id": "f1", "steps": [{"action": "navigate", "target": "/"}]}
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        ok, detail = flow_wiring.run_flow(flow, "http://localhost:3000")
    assert ok is None
    assert "playwright not installed" in detail


# ── _run_wave_flow_wiring_check: wiring into build.py ──

def test_wave_flow_wiring_inert_with_no_flows_declared(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[{"id": "A001", "status": "complete"}])
    assert _run_wave_flow_wiring_check(pcp_dir, [{"name": "widgets"}], 0) == []
    assert _wave_records(pcp_dir, "flow-wiring") == []


def test_wave_flow_wiring_inert_when_spanned_module_incomplete(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy").mkdir(parents=True)
    _mod(pcp_dir, name="a", criteria=[{"id": "A001", "status": "pending"}])
    _mod(pcp_dir, name="b", criteria=[{"id": "B001", "status": "complete"}])
    flows = [{"id": "f1", "modules_spanned": ["a", "b"], "steps": [{"action": "navigate", "target": "/"}]}]
    (pcp_dir / "strategy" / "user_flows.yaml").write_text(yaml.dump({"flows": flows}))
    findings = _run_wave_flow_wiring_check(pcp_dir, [{"name": "b"}], 0)
    assert findings == []
    assert _wave_records(pcp_dir, "flow-wiring") == []


def test_wave_flow_wiring_runs_and_skips_gracefully_without_playwright(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy").mkdir(parents=True)
    _mod(pcp_dir, name="a", criteria=[{"id": "A001", "status": "complete"}])
    _mod(pcp_dir, name="b", criteria=[{"id": "B001", "status": "complete"}])
    flows = [{
        "id": "f1", "modules_spanned": ["a", "b"],
        "base_url": "http://localhost:3000",
        "steps": [{"action": "navigate", "target": "/"}],
    }]
    (pcp_dir / "strategy" / "user_flows.yaml").write_text(yaml.dump({"flows": flows}))
    with patch.dict("sys.modules", {"playwright.sync_api": None}):
        findings = _run_wave_flow_wiring_check(pcp_dir, [{"name": "b"}], 0)
    assert findings == []  # could-not-check, never a finding
    record = _wave_records(pcp_dir, "flow-wiring")[0]
    assert record["control_id"] == "CTRL-040"
    assert record["result"] == "pass"
