from pcp.commands.build import _is_ui_facing_criterion, _build_agent_prompt, _run_design_consistency_check


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
    assert records[0]["error_count"] == 1


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
