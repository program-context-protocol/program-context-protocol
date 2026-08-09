"""pcp verify — the missing 'this is genuinely done' command.

12 criteria on Project O were hand-edited to status: complete with no
verified_by, because pcp pm (spec-authoring) was the only tool anyone reached
for. This is the gated write path that should have existed: re-run the
deterministic check where one exists, require --reason where it doesn't, log
to decision_log.jsonl either way.
"""
import subprocess
import textwrap

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


def test_verification_writes_a_build_cycle_telemetry_record(tmp_path):
    """Restores the build-cycle signal for criteria completed through the
    native-harness path (pcp build-plan + the Workflow tool's agent()), which
    marks work done via `pcp verify` directly rather than build.py's own
    _build_one_criterion -- previously the only place telemetry.record() fired
    for a build-cycle event, so this path went completely dark in
    telemetry.jsonl even while decision_log.jsonl kept recording it."""
    root = _project(tmp_path, check="manual", target=None)
    CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes",
                             "--reason", "commit abc123", "--path", str(root)])
    import json
    lines = (root / ".pcp" / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["cycle"] == "build"
    assert rec["module"] == "billing" and rec["criterion_id"] == "A001"
    assert rec["result"] == "pass"


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


# ── test_passes gate: real fake-test detection (2026-08-08) ──
# `test_passes` previously had ZERO deterministic re-check -- these prove the
# fix actually stops a criterion built on structurally fake tests from
# reaching `complete`, using the SAME classifiers test_composition.py
# already reports in `pcp audit`. Real pytest subprocess, no mocking.

def _project_with_test_file(tmp_path, test_file_body, target="tests/test_thing.py"):
    root = _project(tmp_path, check="test_passes", target=target)
    file_part = target.split("::", 1)[0]
    test_path = root / file_part
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(test_file_body)
    return root


def test_test_passes_refuses_a_grep_shaped_target(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        def test_thing():
            content = open(__file__).read()
            assert "def test_thing" in content
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 1
    assert "FAKE-SHAPED" in result.output
    assert "source-grep-shaped" in result.output
    assert _status(root)["status"] == "pending"


def test_test_passes_refuses_an_assertion_free_target(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        def test_thing():
            x = 1
            y = x + 1
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 1
    assert "zero assertions" in result.output
    assert _status(root)["status"] == "pending"


def test_test_passes_refuses_a_self_mocked_target(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        from unittest.mock import patch

        def compute_score(a, b):
            return a + b

        def test_thing():
            with patch("test_thing.compute_score") as mock_compute:
                mock_compute.return_value = 42
                assert compute_score(1, 2) == 42
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 1
    assert "self-mocked" in result.output
    assert _status(root)["status"] == "pending"


def test_test_passes_accepts_a_real_execution_target(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        def compute_score(a, b):
            return a + b

        def test_thing():
            assert compute_score(1, 2) == 3
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 0
    c = _status(root)
    assert c["status"] == "complete"
    assert c["verified_by"] == "pcp_verify:test_passes"


def test_test_passes_genuine_test_failure_is_refused_and_not_overridable(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        def test_thing():
            assert 1 == 2
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, [
        "verify", "billing", "A001", "--yes", "--path", str(root),
        "--allow-weak-test", "should not matter",
    ])
    assert result.exit_code == 1
    assert "FAILS" in result.output
    assert "FAKE-SHAPED" not in result.output
    assert _status(root)["status"] == "pending"


def test_test_passes_fake_refusal_overridden_with_allow_weak_test(tmp_path):
    root = _project_with_test_file(tmp_path, textwrap.dedent("""
        def test_thing():
            x = 1
    """), target="tests/test_thing.py::test_thing")
    result = CliRunner().invoke(cli, [
        "verify", "billing", "A001", "--yes", "--path", str(root),
        "--allow-weak-test", "accepted as a placeholder, tracked in TICKET-99",
    ])
    assert result.exit_code == 0
    c = _status(root)
    assert c["status"] == "complete"
    assert "weak-override" in c["verified_by"]

    import json
    lines = (root / ".pcp" / "decision_log.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["category"] == "weak-test-override"
    assert "TICKET-99" in rec["evidence"]


def test_test_passes_without_target_falls_back_to_manual_path(tmp_path):
    """No `target` declared -- unchanged pre-existing behavior, backward
    compatible with every project that predates this gate."""
    root = _project(tmp_path, check="test_passes", target=None)
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 2
    assert "--reason is required" in result.output


def test_test_passes_missing_target_file_refuses_without_fake_shape_message(tmp_path):
    root = _project(tmp_path, check="test_passes", target="tests/test_missing.py::test_x")
    result = CliRunner().invoke(cli, ["verify", "billing", "A001", "--yes", "--path", str(root)])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "FAKE-SHAPED" not in result.output
