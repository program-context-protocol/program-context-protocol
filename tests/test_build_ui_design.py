import yaml
from unittest.mock import patch

from pcp.commands.build import (
    _is_ui_facing_criterion, _build_agent_prompt, _run_design_consistency_check,
    _run_design_justification_check,
)


def test_is_ui_facing_criterion_detects_ui_keywords():
    assert _is_ui_facing_criterion({"description": "Dashboard renders coverage % for test estate"})
    assert _is_ui_facing_criterion({"description": "Review portal displays AI-generated diff"})
    assert _is_ui_facing_criterion({"description": "Settings form validates input client-side"})


def test_is_ui_facing_criterion_false_for_backend_only():
    assert not _is_ui_facing_criterion({"description": "API returns correct Tier 1+2 % for test app.yaml set"})
    assert not _is_ui_facing_criterion({"description": "Auth Broker handles Kerberos ticket renewal"})


def test_is_ui_facing_criterion_handles_missing_description():
    assert not _is_ui_facing_criterion({})


def test_build_agent_prompt_includes_design_system_hint_for_ui_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "IT admin dashboard renders per-app tier assignment"}
    prompt = _build_agent_prompt(pcp_dir, "it-admin-dashboard", criterion, {})
    assert "design_system.md" in prompt
    assert "pcp-ui-design" in prompt


def test_build_agent_prompt_omits_design_system_hint_for_backend_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "Coverage calculation API returns correct percentage"}
    prompt = _build_agent_prompt(pcp_dir, "control-plane", criterion, {})
    assert "design_system.md" not in prompt
    assert "pcp-ui-design" not in prompt


# ── _run_design_consistency_check: PCP Design lifecycle, stage 4 (Verify) ──

def _ctx(module="ui-mod", criterion_id="A001", attempt=1):
    return {"module": module, "submodule": None, "criterion_id": criterion_id, "attempt": attempt, "files": []}


def test_design_consistency_check_noop_for_non_ui_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "API returns correct percentage"}
    # Should not raise, should not require design_system.md at all.
    _run_design_consistency_check(pcp_dir, tmp_path, criterion, _ctx())


def test_design_consistency_check_noop_when_design_system_not_established(tmp_path):
    from pcp import telemetry

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_system.md").write_text("# Design System\n\n## Color\n\n(not yet established)\n")
    criterion = {"id": "A001", "description": "Dashboard renders coverage", "target": "app.py"}
    (tmp_path / "app.py").write_text("color = '#ff0000'\n")
    _run_design_consistency_check(pcp_dir, tmp_path, criterion, _ctx())
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "design-consistency"]
    assert len(records) == 1
    assert records[0]["result"] == "skipped"


def test_design_consistency_check_flags_hardcoded_hex_when_system_established(tmp_path):
    from pcp import telemetry

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_system.md").write_text("# Design System\n\n## Color\n| --accent | #0f6e70 |\n")
    criterion = {"id": "A001", "description": "Dashboard renders coverage", "target": "app.py"}
    (tmp_path / "app.py").write_text("color = '#ff0000'\n")
    _run_design_consistency_check(pcp_dir, tmp_path, criterion, _ctx())
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "design-consistency"]
    assert len(records) == 1
    assert records[0]["result"] == "block"
    # Two findings since 2026-07-17: the hardcoded hex AND the positive check
    # (file references zero named --tokens from the established system).
    assert records[0]["error_count"] == 2
    assert any("hardcoded hex" in e for e in records[0]["errors"])
    assert any("references none" in e for e in records[0]["errors"])


def test_design_consistency_check_passes_when_no_hardcoded_colors(tmp_path):
    from pcp import telemetry

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_system.md").write_text("# Design System\n\n## Color\n| --accent | #0f6e70 |\n")
    criterion = {"id": "A001", "description": "Dashboard renders coverage", "target": "app.py"}
    (tmp_path / "app.py").write_text("color = var(--accent)\n")
    _run_design_consistency_check(pcp_dir, tmp_path, criterion, _ctx())
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "design-consistency"]
    assert len(records) == 1
    assert records[0]["result"] == "pass"


# ── _run_design_justification_check: stage 4, substance check ──

def _mod(pcp_dir, name="widgets", design_justification=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    acc_path = mod_dir / "acceptance.yaml"
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    if design_justification is not None:
        criterion["design_justification"] = design_justification
    acc_path.write_text(yaml.dump({"criteria": [criterion]}))
    return {"name": name, "acc_path": acc_path}


def test_design_justification_check_noop_for_non_ui_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir)
    criterion = {"id": "A001", "description": "API returns correct percentage"}  # not UI-facing
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []
    mock_call.assert_not_called()


def test_design_justification_check_noop_when_field_absent(tmp_path):
    """Rung 1 (Built, Hidden) -- design_audit.py's rollup already surfaces
    this; this check's job starts once a design_justification exists."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, design_justification=None)
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []
    mock_call.assert_not_called()


def test_design_justification_check_flags_lazy_fill_and_survives_verify(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, design_justification={
        "checklist_passed": ["x"], "jtbd_framing": "shows the dashboard", "deviations_from_system": "",
    })
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    judge_response = {"substantive": False, "reason": "jtbd_framing just restates the description"}
    verify_response = {"verdicts": [{"index": 0, "refuted": False, "reason": "grounded in the submitted block"}]}
    with patch("pcp.commands.build.llm.call_json",
               side_effect=[(judge_response, {"model": "haiku"}), (verify_response, {"model": "haiku"})]):
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert len(findings) == 1
    assert "lazily filled" in findings[0]

    from pcp import telemetry
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "design-justification"]
    assert records[-1]["result"] == "block"


def test_design_justification_check_drops_ungrounded_finding_via_verify(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, design_justification={
        "checklist_passed": ["both-themes", "grounded-in-subject"],
        "jtbd_framing": "when a user submits invalid input, this shows exactly what to fix",
        "deviations_from_system": "",
    })
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    judge_response = {"substantive": False, "reason": "seems generic"}
    verify_response = {"verdicts": [{"index": 0, "refuted": True, "reason": "jtbd_framing is a real conditional"}]}
    with patch("pcp.commands.build.llm.call_json",
               side_effect=[(judge_response, {"model": "haiku"}), (verify_response, {"model": "haiku"})]):
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []


def test_design_justification_check_substantive_passes_no_verify_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, design_justification={
        "checklist_passed": ["both-themes"],
        "jtbd_framing": "when a user is on mobile, this collapses the sidebar",
        "deviations_from_system": "",
    })
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    judge_response = {"substantive": True, "reason": "real JTBD framing"}
    with patch("pcp.commands.build.llm.call_json", return_value=(judge_response, {"model": "haiku"})) as mock_call:
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []
    assert mock_call.call_count == 1  # no verify call needed -- nothing to verify


def test_design_justification_check_fails_open_on_call_error(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, design_justification={"checklist_passed": [], "jtbd_framing": "", "deviations_from_system": ""})
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    with patch("pcp.commands.build.llm.call_json", side_effect=RuntimeError("timeout")):
        findings = _run_design_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []
    from pcp import telemetry
    records = [r for r in telemetry.load(pcp_dir) if r.get("check") == "design-justification"]
    assert records[-1]["result"] == "error"
