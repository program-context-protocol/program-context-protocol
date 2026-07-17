"""Phase 2 evidence-platform items (2026-07-17 build plan)."""

import base64
import json

from click.testing import CliRunner

from pcp import telemetry
from pcp.attest import build_statements, export_attestations, STATEMENT_TYPE
from pcp.cli import cli


def _seed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "src.py").write_text("x = 1\n")
    telemetry.record(
        pcp_dir, cycle="qa", check="lint", control_id="CTRL-002",
        module="m", criterion_id="A1", files=["src.py"], result="pass",
    )
    return pcp_dir


def test_statements_are_in_toto_v1_shaped(tmp_path):
    pcp_dir = _seed(tmp_path)
    stmts = build_statements(pcp_dir)
    assert len(stmts) == 1
    s = stmts[0]
    assert s["_type"] == STATEMENT_TYPE
    assert s["subject"][0]["name"] == "src.py"
    assert len(s["subject"][0]["digest"]["sha256"]) == 64
    assert s["predicate"]["control_id"] == "CTRL-002"


def test_export_writes_unsigned_dsse_envelopes(tmp_path):
    pcp_dir = _seed(tmp_path)
    out, count, note = export_attestations(pcp_dir, sign=False)
    assert count == 1
    assert "UNSIGNED" in note
    env = json.loads(out.read_text().splitlines()[0])
    assert env["payloadType"] == "application/vnd.in-toto+json"
    assert env["signatures"] == []
    decoded = json.loads(base64.b64decode(env["payload"]))
    assert decoded["_type"] == STATEMENT_TYPE


def test_records_without_files_or_control_are_skipped(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="qa", check="gate", control_id=None, files=[], result="pass")
    telemetry.record(pcp_dir, cycle="build", check=None, control_id="CTRL-001", files=["gone.py"], result="pass")
    assert build_statements(pcp_dir) == []


def test_provenance_attest_flag(tmp_path):
    pcp_dir = _seed(tmp_path)
    (pcp_dir / "controls.yaml").write_text("version: '1.0'\ncontrols: []\n")
    result = CliRunner().invoke(cli, ["provenance", "--path", str(tmp_path), "--attest"])
    assert result.exit_code == 0
    assert (pcp_dir / "attestations.jsonl").exists()
    assert (pcp_dir / "attestations.meta.json").exists()


def test_provenance_contains_auditability_card(tmp_path):
    pcp_dir = _seed(tmp_path)
    (pcp_dir / "controls.yaml").write_text("version: '1.0'\ncontrols: []\n")
    result = CliRunner().invoke(cli, ["provenance", "--path", str(tmp_path)])
    assert result.exit_code == 0
    text = (pcp_dir / "provenance.md").read_text()
    assert "## Auditability Card" in text
    assert "Action recoverability" in text
    assert "Evidence integrity" in text
