import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.design_audit import build_design_audit, write_design_audit, _classify_rung


def _write_module(pcp_dir, name, criteria):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    (mod_dir / "acceptance.yaml").write_text(yaml.dump({"module": name, "criteria": criteria}))


def test_classify_rung_1_built_hidden():
    assert _classify_rung({"id": "A001", "description": "Dashboard renders coverage"}) == 1


def test_classify_rung_2_exposed_undiscoverable():
    c = {"id": "A001", "description": "Dashboard renders coverage", "design_justification": {}}
    assert _classify_rung(c) == 2


def test_classify_rung_3_exposed_discoverable():
    c = {"id": "A001", "description": "Dashboard renders coverage",
         "design_justification": {"checklist_passed": ["both-themes"], "jtbd_framing": "shows coverage"}}
    assert _classify_rung(c) == 3


def test_classify_rung_4_exposed_enriched():
    c = {"id": "A001", "description": "Dashboard renders coverage",
         "design_justification": {"checklist_passed": ["both-themes"],
                                   "jtbd_framing": "when a PM worries coverage is slipping, this screen shows the real number"}}
    assert _classify_rung(c) == 4


def test_build_design_audit_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    data = build_design_audit(pcp_dir)
    assert data["modules"] == []
    assert data["total_ui_criteria"] == 0


def test_build_design_audit_skips_non_ui_criteria(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "backend", [
        {"id": "A001", "description": "API returns correct percentage", "check": "manual", "status": "pending"},
    ])
    data = build_design_audit(pcp_dir)
    assert data["modules"] == []


def test_build_design_audit_aggregates_ui_criteria_by_rung(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "admin", [
        {"id": "A001", "description": "Admin dashboard renders per-app state", "check": "manual", "status": "pending"},
        {"id": "A002", "description": "Settings form displays validation errors", "check": "manual", "status": "pending",
         "design_justification": {"checklist_passed": ["grounded-in-subject"],
                                   "jtbd_framing": "when a user submits invalid input, this shows exactly what to fix"}},
    ])
    data = build_design_audit(pcp_dir)
    assert data["total_ui_criteria"] == 2
    assert data["rung_counts"][1] == 1
    assert data["rung_counts"][4] == 1
    mod = data["modules"][0]
    assert mod["module"] == "admin"
    ids_by_rung = {c["id"]: c["rung"] for c in mod["criteria"]}
    assert ids_by_rung["A001"] == 1
    assert ids_by_rung["A002"] == 4


def test_write_design_audit_renders_markdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "admin", [
        {"id": "A001", "description": "Dashboard renders coverage", "check": "manual", "status": "pending"},
    ])
    out = write_design_audit(pcp_dir)
    assert out.exists()
    content = out.read_text()
    assert "Feature Exposure Ladder" in content
    assert "Module: `admin`" in content
    assert "HEART" in content


def test_design_audit_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    assert '"rung_counts"' in result.output


def test_design_audit_cli_writes_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert (pcp_dir / "design_audit.md").exists()


def test_design_audit_cli_no_pcp_dir_exits(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["design-audit", "--path", str(tmp_path)])
    assert result.exit_code == 2
