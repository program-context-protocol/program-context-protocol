"""CTRL-028: UI kit recipe completeness + import verification, added
2026-07-20. PCP doesn't build/maintain UI component code (shadcn/ui already
solves that) -- this is the thin layer PCP owns: archetype-to-organism
recipes + deterministic import-based usage verification."""

import yaml

from pcp import telemetry
from pcp.commands.build import _run_wave_ui_kit_check


def _mod(pcp_dir, name="widgets", criteria=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    acc_path = mod_dir / "acceptance.yaml"
    acc_path.write_text(yaml.dump({"criteria": criteria or []}))
    return {"name": name, "acc_path": acc_path}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


_RECIPES = {
    "kit": "shadcn",
    "organisms": {
        "data-table": {"component": None, "import_path_hint": "components/ui/table"},
        "kpi-tile": {"component": "card", "import_path_hint": "components/ui/card"},
        "chart-panel": {"component": "chart", "import_path_hint": "components/ui/chart"},
    },
    "archetypes": {
        "dashboard": ["kpi-tile", "chart-panel", "data-table"],
    },
}


def _write_recipes(pcp_dir, recipes=_RECIPES):
    (pcp_dir / "ui_kit_recipes.yaml").write_text(yaml.dump(recipes))


def test_inert_without_recipes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": []},
    ])
    assert _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0) == []
    assert not [r for r in _qa_records(pcp_dir) if r["check"] == "wave-ui-kit"]


def test_flags_incomplete_recipe(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": ["kpi-tile"]},
    ])
    findings = _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    assert "chart-panel" in findings[0] and "data-table" in findings[0]


def test_recipe_complete_no_finding(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": ["kpi-tile", "chart-panel", "data-table"],
         "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text(
        "import { Card } from 'components/ui/card'\n"
        "import { Chart } from 'components/ui/chart'\n"
        "import { Table } from 'components/ui/table'\n"
    )
    assert _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_flags_unverified_import(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete",
         "ui_organisms": ["kpi-tile"], "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("def render(): return 'ok'\n")
    findings = _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0)
    assert len(findings) == 1
    assert "kpi-tile" in findings[0] and "components/ui/card" in findings[0]


def test_organism_with_no_import_hint_never_flagged(tmp_path):
    """data-table has component: null (hand-composed, per shadcn's own docs)
    -- import_path_hint still anchors on the closest real primitive, so a
    file that imports it should pass, not be treated as unverifiable."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "List renders devices", "status": "complete",
         "ui_organisms": ["data-table"], "target": "app.py"},
    ])
    (tmp_path / "app.py").write_text("import { Table } from 'components/ui/table'\n")
    assert _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_skips_non_ui_and_incomplete_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "API returns correct percentage", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": []},
        {"id": "A2", "description": "Dashboard renders coverage", "status": "pending",
         "screen_archetypes": ["dashboard"], "ui_organisms": []},
    ])
    assert _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0) == []


def test_records_telemetry_advisory(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_recipes(pcp_dir)
    _mod(pcp_dir, criteria=[
        {"id": "A1", "description": "Dashboard renders coverage", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": ["kpi-tile"]},
    ])
    _run_wave_ui_kit_check(pcp_dir, [{"name": "widgets"}], 0)
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "wave-ui-kit"][0]
    assert record["control_id"] == "CTRL-028"
    assert record["result"] == "advisory"  # advisory: ran, found something, deliberately did not block.
    # NOT "pass" -- that value is what `pcp provenance` reads, and claiming
    # a clean pass for a check that found things falsifies the audit trail.
