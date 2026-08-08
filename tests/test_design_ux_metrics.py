"""Three new PCP Design metrics, added 2026-07-20: nav_depth / click-depth
(CTRL-025), feature customization structural check (CTRL-026), and the
top-menu-bar convention check (CTRL-027, desktop_app archetype only)."""

import yaml

from pcp import telemetry
from pcp.commands.build import (
    _run_customization_check, _run_wave_nav_depth_check, _run_wave_menu_bar_check,
)
from pcp.commands.design_audit import build_design_audit


def _mod(pcp_dir, name="widgets", criteria=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    acc_path = mod_dir / "acceptance.yaml"
    acc_path.write_text(yaml.dump({"criteria": criteria or []}))
    return {"name": name, "acc_path": acc_path}


def _ctx(module="widgets", criterion_id="A001", attempt=1):
    return {"module": module, "submodule": None, "criterion_id": criterion_id, "attempt": attempt, "files": []}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


# ── CTRL-026: feature customization structural check ──

def test_customization_check_noop_for_non_ui_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "API returns correct percentage"}
    mod = _mod(pcp_dir, criteria=[criterion])
    _run_customization_check(pcp_dir, mod, criterion, _ctx())
    assert not [r for r in _qa_records(pcp_dir) if r["check"] == "customization"]


def test_customization_check_skips_when_not_declared_customizable(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A001", "description": "Dashboard renders coverage"}
    mod = _mod(pcp_dir, criteria=[criterion])
    _run_customization_check(pcp_dir, mod, criterion, _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "customization"][0]
    assert record["control_id"] == "CTRL-026"
    assert record["result"] == "skipped"


def test_customization_check_flags_true_with_no_notes_and_no_signal(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {
        "id": "A001", "description": "Dashboard renders coverage", "target": "app.py",
        "design_justification": {"customizable": True},
    }
    mod = _mod(pcp_dir, criteria=[criterion])
    (tmp_path / "app.py").write_text("def render():\n    return 'ok'\n")
    _run_customization_check(pcp_dir, mod, criterion, _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "customization"][0]
    assert record["result"] == "block"
    assert record["error_count"] == 2  # empty notes AND no keyword signal
    assert any("customization_notes is" in e for e in record["errors"])
    assert any("no settings/preference/config-shaped signal" in e for e in record["errors"])


def test_customization_check_passes_with_real_notes_and_signal(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {
        "id": "A001", "description": "Dashboard renders coverage", "target": "app.py",
        "design_justification": {
            "customizable": True,
            "customization_notes": "Users can choose which columns to show and reorder them",
        },
    }
    mod = _mod(pcp_dir, criteria=[criterion])
    (tmp_path / "app.py").write_text("def render_settings_panel():\n    return preferences\n")
    _run_customization_check(pcp_dir, mod, criterion, _ctx())
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "customization"][0]
    assert record["result"] == "pass"
    assert record["error_count"] == 0


# ── CTRL-025: navigation depth (click-depth) ──

def test_nav_depth_flags_missing_declaration(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete"},
    ])
    findings = _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    assert "no nav_depth declared" in findings[0]


def test_nav_depth_flags_exceeding_threshold(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete", "nav_depth": 5},
    ])
    findings = _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    assert "nav_depth=5" in findings[0]
    assert "3-click threshold" in findings[0]


def test_nav_depth_clean_within_threshold(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete", "nav_depth": 2},
    ])
    assert _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_nav_depth_skips_non_ui_and_incomplete_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "API returns correct percentage", "status": "complete"},
        {"id": "A2", "description": "Dashboard renders coverage", "status": "pending"},
    ])
    assert _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_nav_depth_skips_criterion_declaring_non_ui_exposure(tmp_path):
    """Fixed 2026-08-08 (Project W dogfood): a backend-only criterion whose
    description happens to contain a UI keyword ("dashboard") used to get
    flagged for missing nav_depth even after explicitly opting out via
    exposure.mode -- irrelevant noise on every non-UI build. CTRL-039 itself
    must still see this criterion (it validates the justification), only
    nav-depth's own advisory should stop firing on it."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Internal metrics dashboard data pulled by a cron job, no screen",
         "status": "complete", "exposure": {"mode": "internal", "justification": "Background job only, no UI."}},
    ])
    assert _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_nav_depth_records_telemetry_advisory(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete"},
    ])
    _run_wave_nav_depth_check(pcp_dir, [{"name": "widgets"}], 0)
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "wave-nav-depth"][0]
    assert record["control_id"] == "CTRL-025"
    assert record["result"] == "advisory"  # advisory: ran, found something, deliberately did not block.
    # NOT "pass" -- that value is what `pcp provenance` reads, and claiming
    # a clean pass for a check that found things falsifies the audit trail.


# ── CTRL-027: top menu bar convention (desktop_app archetype only) ──

def test_menu_bar_inert_without_design_conventions_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete", "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("def render(): return 'ok'\n")
    assert _run_wave_menu_bar_check(pcp_dir, [{"name": "widgets"}], 0) == []
    assert not [r for r in _qa_records(pcp_dir) if r["check"] == "wave-menu-bar"]


def test_menu_bar_inert_for_web_app_archetype(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_conventions.yaml").write_text(yaml.dump({"ui_archetype": "web_app"}))
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete", "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("def render(): return 'ok'\n")
    assert _run_wave_menu_bar_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_menu_bar_flags_missing_menus_for_desktop_app(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_conventions.yaml").write_text(yaml.dump({
        "ui_archetype": "desktop_app",
        "top_menu_bar": {"required_menus": ["File", "Edit", "View", "Help"]},
    }))
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Main app shell renders", "status": "complete", "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("menu_bar = ['File', 'Edit']\n")
    findings = _run_wave_menu_bar_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    missing_clause = findings[0].split("not found")[0]
    assert "View" in missing_clause and "Help" in missing_clause


def test_menu_bar_passes_when_all_menus_present(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_conventions.yaml").write_text(yaml.dump({
        "ui_archetype": "desktop_app",
        "top_menu_bar": {"required_menus": ["File", "Edit"]},
    }))
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Main app shell renders", "status": "complete", "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("menu_bar = ['File', 'Edit', 'View', 'Help']\n")
    assert _run_wave_menu_bar_check(pcp_dir, [{"name": "widgets"}], 0) == []


# ── design_audit rollup ──

def test_design_audit_rolls_up_nav_depth_and_customization(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "nav_depth": 2,
         "design_justification": {"customizable": True}},
        {"id": "A2", "description": "Settings screen renders preferences", "nav_depth": 6,
         "design_justification": {"customizable": False}},
    ])
    data = build_design_audit(pcp_dir)
    assert data["nav_depth"]["declared"] == 2
    assert data["nav_depth"]["max"] == 6
    assert data["nav_depth"]["within_threshold_pct"] == 0.5
    assert data["customization"]["customizable_count"] == 1
    assert data["customization"]["customizable_pct"] == 0.5
    assert data["ui_archetype"] is None


def test_design_audit_surfaces_desktop_app_archetype(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "design_conventions.yaml").write_text(yaml.dump({"ui_archetype": "desktop_app"}))
    data = build_design_audit(pcp_dir)
    assert data["ui_archetype"] == "desktop_app"
