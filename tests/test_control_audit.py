"""Control-catalog self-audit (2026-07-21 self-evaluation gap-close):
which cataloged CTRL checks have never fired across recorded telemetry
history -- retire/merge review candidates, never auto-pruned."""

import json

import yaml
from click.testing import CliRunner

from pcp import control_audit
from pcp.cli import cli


def _write_controls(pcp_dir, controls):
    (pcp_dir / "controls.yaml").write_text(yaml.dump({"version": "1.0", "controls": controls}))


def _append_telemetry(pcp_dir, record):
    with open(pcp_dir / "telemetry.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


def _control(cid, name="x", layer="build-loop", enforcement="advisory"):
    return {"id": cid, "name": name, "layer": layer, "enforcement": enforcement,
            "mechanism": "m", "tool": "n/a", "description": "d", "ssdf_practice": ["PW.1.1"]}


def test_build_audit_empty_without_controls_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert control_audit.build_control_audit(pcp_dir) == {}


def test_insufficient_data_below_min_runs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    for _ in range(5):
        _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "pass", "error_count": 0})
    audit = control_audit.build_control_audit(pcp_dir)
    assert audit["CTRL-001"]["signal"] == "insufficient-data"
    assert audit["CTRL-001"]["total_runs"] == 5


def test_never_fired_at_or_above_min_runs_with_zero_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    for _ in range(25):
        _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "pass", "error_count": 0})
    audit = control_audit.build_control_audit(pcp_dir)
    assert audit["CTRL-001"]["signal"] == "never-fired"
    assert audit["CTRL-001"]["total_runs"] == 25
    assert audit["CTRL-001"]["findings"] == 0


def test_active_when_at_least_one_finding(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    for _ in range(24):
        _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "pass", "error_count": 0})
    _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "block", "error_count": 2})
    audit = control_audit.build_control_audit(pcp_dir)
    assert audit["CTRL-001"]["signal"] == "active"
    assert audit["CTRL-001"]["findings"] == 1
    assert audit["CTRL-001"]["rate"] == round(1 / 25, 3)


def test_control_never_recorded_in_telemetry_is_zero_runs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-099")])
    audit = control_audit.build_control_audit(pcp_dir)
    assert audit["CTRL-099"]["total_runs"] == 0
    assert audit["CTRL-099"]["signal"] == "insufficient-data"


def test_render_markdown_lists_never_fired_section(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001", name="Test suite must pass")])
    for _ in range(25):
        _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "pass", "error_count": 0})
    audit = control_audit.build_control_audit(pcp_dir)
    md = control_audit.render_markdown(audit)
    assert "CTRL-001" in md
    assert "Test suite must pass" in md
    assert "Never-fired" in md


def test_write_control_audit_creates_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    control_audit.write_control_audit(pcp_dir)
    assert (pcp_dir / "control_audit.md").exists()


# ── CLI ──

def test_cli_reports_never_fired_controls(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001", name="Never fires")])
    for _ in range(25):
        _append_telemetry(pcp_dir, {"control_id": "CTRL-001", "result": "pass", "error_count": 0})
    runner = CliRunner()
    result = runner.invoke(cli, ["control-audit", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "CTRL-001" in result.output
    assert "Never fires" in result.output


def test_cli_json_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    runner = CliRunner()
    result = runner.invoke(cli, ["control-audit", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert "CTRL-001" in data


def test_cli_errors_without_pcp_dir(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["control-audit", "--path", str(tmp_path)])
    assert result.exit_code == 2


# ── --sync: additive-only catalog refresh (2026-07-22) ──

def test_sync_catalog_appends_missing_ids_only(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001", name="Test suite must pass")])
    added = control_audit.sync_catalog(pcp_dir)
    assert "CTRL-002" in added
    assert "CTRL-035" in added
    assert "CTRL-001" not in added  # already present, never touched

    data = yaml.safe_load((pcp_dir / "controls.yaml").read_text())
    ids = [c["id"] for c in data["controls"]]
    assert len(ids) == len(set(ids))  # no duplicates
    assert "CTRL-035" in ids


def test_sync_catalog_never_rewrites_existing_entry(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001", name="Hand-edited custom name", enforcement="advisory")])
    control_audit.sync_catalog(pcp_dir)
    data = yaml.safe_load((pcp_dir / "controls.yaml").read_text())
    c001 = next(c for c in data["controls"] if c["id"] == "CTRL-001")
    assert c001["name"] == "Hand-edited custom name"
    assert c001["enforcement"] == "advisory"


def test_sync_catalog_noop_when_already_current(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    from pcp.commands.init import CONTROLS_TEMPLATE
    (pcp_dir / "controls.yaml").write_text(CONTROLS_TEMPLATE)
    assert control_audit.sync_catalog(pcp_dir) == []


def test_sync_catalog_noop_without_controls_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert control_audit.sync_catalog(pcp_dir) == []
    assert not (pcp_dir / "controls.yaml").exists()


def test_cli_sync_flag_reports_added_ids(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [_control("CTRL-001")])
    runner = CliRunner()
    result = runner.invoke(cli, ["control-audit", "--path", str(tmp_path), "--sync"])
    assert result.exit_code == 0
    assert "CTRL-035" in result.output


def test_cli_sync_flag_reports_noop_when_current(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    from pcp.commands.init import CONTROLS_TEMPLATE
    (pcp_dir / "controls.yaml").write_text(CONTROLS_TEMPLATE)
    runner = CliRunner()
    result = runner.invoke(cli, ["control-audit", "--path", str(tmp_path), "--sync"])
    assert result.exit_code == 0
    assert "already current" in result.output
