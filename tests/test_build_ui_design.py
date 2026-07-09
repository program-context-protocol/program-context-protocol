from pcp.commands.build import _is_ui_facing_criterion, _build_agent_prompt


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
