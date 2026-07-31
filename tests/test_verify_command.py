"""pcp verify — the missing 'this is genuinely done' command.

12 criteria on Project O were hand-edited to status: complete with no
verified_by, because pcp pm (spec-authoring) was the only tool anyone reached
for. This is the gated write path that should have existed: re-run the
deterministic check where one exists, require --reason where it doesn't, log
to decision_log.jsonl either way.
"""
import subprocess

import yaml
from click.testing import CliRunner

from pcp.cli import cli


def _project(tmp_path, check="file_exists", target="src/f.py", status="pending",
             criterion_extra=None):
    root = tmp_path / "p"
    mod = root / ".pcp" / "strategy" / "modules" / "billing"
    mod.mkdir(parents=True)
    crit = {"id": "A001", "description": "charge endpoint", "check": check, "status": status}
    if target:
        crit["target"] = target
    if criterion_extra:
        crit.update(criterion_extra)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing", "criteria": [crit],
    }))
    (mod / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing", "description": "d"}))
    return root


def _status(root):
    d = yaml.safe_load((root / ".pcp" / "strategy" / "modules" / "billing" / "acceptance.yaml").read_text())
    return d["criteria"][0]


def test_file_exists_check_verifies_automatically_when_the_file_is_there(tmp_path):
    root = _project(tmp_path, check="file_exists", target="src/f.py")
    (root / "src").mkdir(); (root / "src" / "f.py").write_text("x")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 0
    c = _status(root)
    assert c["status"] == "complete"
    assert c["verified_by"] == "pcp_verify:file_exists"


def test_file_exists_check_refuses_when_the_file_is_missing(tmp_path):
    root = _project(tmp_path, check="file_exists", target="src/f.py")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 1
    assert "NOT" in result.output and "complete" in result.output
    assert _status(root)["status"] == "pending"     # refused, nothing written


def test_manual_check_requires_a_reason(tmp_path):
    root = _project(tmp_path, check="manual", target=None)
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 2
    assert "--reason is required" in result.output
    assert _status(root)["status"] == "pending"


def test_manual_check_with_a_reason_succeeds(tmp_path):
    root = _project(tmp_path, check="manual", target=None)
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes",
                                      "--reason", "confirmed via commit abc123", "--path", str(root)])
    assert result.exit_code == 0
    c = _status(root)
    assert c["status"] == "complete" and c["verified_by"] == "pcp_verify:manual"


def test_verification_is_logged_to_decision_log(tmp_path):
    root = _project(tmp_path, check="manual", target=None)
    CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes",
                             "--reason", "commit abc123", "--path", str(root)])
    import json
    lines = (root / ".pcp" / "decision_log.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["module"] == "billing" and rec["criterion_id"] == "A001"
    assert "commit abc123" in rec["evidence"]


def test_already_verified_complete_is_a_no_op(tmp_path):
    root = _project(tmp_path, check="manual", target=None, status="complete",
                    criterion_extra={"verified_by": "pcp_build"})
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 0
    assert "Nothing to do" in result.output


def test_unknown_criterion_errors_cleanly(tmp_path):
    root = _project(tmp_path)
    result = CliRunner().invoke(cli, ["verify", "billing", "A999", "--yes", "--path", str(root)])
    assert result.exit_code == 2


def test_declining_the_confirm_writes_nothing(tmp_path):
    root = _project(tmp_path, check="manual", target=None)
    result = CliRunner().invoke(cli, ["verify", "billing", "A001",
                                      "--reason", "x"], input="n\n",
                                catch_exceptions=False, args=None) if False else \
             CliRunner().invoke(cli, ["verify", "billing", "A001", "--reason", "x", "--path", str(root)],
                                input="n\n")
    assert _status(root)["status"] == "pending"
