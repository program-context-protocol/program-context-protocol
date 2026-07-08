import json

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.provenance import build_provenance
from pcp import telemetry


def _write_controls(pcp_dir, controls):
    (pcp_dir / "controls.yaml").write_text(yaml.dump({"controls": controls}))


def test_build_provenance_empty_project(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    result = build_provenance(pcp_dir)
    assert result["per_file"] == {}
    assert result["bypasses"] == []


def test_build_provenance_aggregates_qa_records_per_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-001", result="pass", files=["a.py"])
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-002", result="block", files=["a.py", "b.py"])
    result = build_provenance(pcp_dir)
    assert result["per_file"]["a.py"]["CTRL-001"]["result"] == "pass"
    assert result["per_file"]["a.py"]["CTRL-002"]["result"] == "block"
    assert result["per_file"]["b.py"]["CTRL-002"]["result"] == "block"
    assert result["per_control"]["CTRL-001"]["total"] == 1
    assert result["per_control"]["CTRL-002"]["block"] == 1


def test_build_provenance_falls_back_to_check_name_for_old_records(tmp_path):
    """Telemetry records written before control_id existed should still map
    to a control via the check-name fallback."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="qa", check="gate", result="pass", files=["a.py"])
    result = build_provenance(pcp_dir)
    assert result["per_file"]["a.py"]["CTRL-006"]["result"] == "pass"


def test_standing_gap_when_every_outcome_is_skipped(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [{"id": "CTRL-003", "name": "SAST"}])
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-003", result="skipped", files=["a.py"])
    result = build_provenance(pcp_dir)
    assert "CTRL-003" in result["standing_gap_cids"]


def test_never_exercised_control_flagged_except_ctrl_010(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [{"id": "CTRL-001", "name": "Tests"}, {"id": "CTRL-010", "name": "Bypass accountability"}])
    result = build_provenance(pcp_dir)
    assert "CTRL-001" in result["never_exercised_cids"]
    assert "CTRL-010" not in result["never_exercised_cids"]


def test_bypasses_included_in_provenance(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "bypass_log.yaml").write_text(yaml.dump({
        "bypasses": [{"timestamp": "t", "reason": "known issue", "rules_bypassed": ["R001"]}]
    }))
    result = build_provenance(pcp_dir)
    assert len(result["bypasses"]) == 1


def test_provenance_cli_writes_markdown(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_controls(pcp_dir, [{"id": "CTRL-001", "name": "Test suite", "ssdf_practice": ["PW.7"], "enforcement": "hard_block"}])
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-001", result="pass", files=["a.py"])

    runner = CliRunner()
    result = runner.invoke(cli, ["provenance", "--path", str(tmp_path)])
    assert result.exit_code == 0
    md = (pcp_dir / "provenance.md").read_text()
    assert "PCP Audit Evidence" in md
    assert "a.py" in md
    assert "SSDF Crosswalk" in md


def test_provenance_cli_json(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="qa", control_id="CTRL-001", result="pass", files=["a.py"])
    runner = CliRunner()
    result = runner.invoke(cli, ["provenance", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert data["per_file"]["a.py"]["CTRL-001"]["result"] == "pass"
