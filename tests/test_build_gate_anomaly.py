"""Cross-criterion anomaly detection, 2026-07-21 -- see docs/CHANGELOG.md
and .pcp/strategy for context. Two related gaps found dogfooding:
(1) a squatted DB port made the test-suite gate "time out" identically
across several criteria before a human caught it; (2) a semgrep tool
failure surfaced as "SAST found issues" with an empty evidence file.
Neither had a durable, unattended-run-safe detection mechanism -- these
tests cover the three tripwires added in response."""

from unittest.mock import patch

from pcp.commands.build import _BuildBudget, _qa_record, _report_gate_skip_anomaly
from pcp import escalations, telemetry

CTX = {"attempt": 1, "module": "add", "criterion_id": "A001"}


def test_test_timeout_streak_trips_at_threshold():
    budget = _BuildBudget(10)
    assert budget.record_test_timeout_signal(True) is False
    assert budget.record_test_timeout_signal(True) is False
    assert budget.record_test_timeout_signal(True) is True  # default threshold 3
    # already tripped -- doesn't fire again even if the streak keeps growing
    assert budget.record_test_timeout_signal(True) is False


def test_test_timeout_streak_resets_on_a_real_pass():
    budget = _BuildBudget(10)
    budget.record_test_timeout_signal(True)
    budget.record_test_timeout_signal(True)
    budget.record_test_timeout_signal(False)  # a real pass in between
    assert budget.infra_signal_streak == 0
    assert budget.record_test_timeout_signal(True) is False  # streak restarted, not near threshold


def test_gate_skip_streak_tracks_lint_and_sast_independently():
    budget = _BuildBudget(10)
    budget.record_gate_skip_signal("lint", True)
    budget.record_gate_skip_signal("lint", True)
    assert budget.record_gate_skip_signal("lint", True) is True
    # sast has its own independent streak, unaffected by lint's
    assert budget.record_gate_skip_signal("sast", True) is False
    assert budget.gate_skip_streaks["sast"] == 1


def test_report_gate_skip_anomaly_ignores_tool_not_installed():
    """tool=None means the linter genuinely isn't installed -- expected,
    stable project config, must never count toward the anomaly streak."""
    budget = _BuildBudget(10)
    for _ in range(5):
        _report_gate_skip_anomaly(budget, "lint", None, False)
    assert budget.gate_skip_streaks.get("lint", 0) == 0


def test_report_gate_skip_anomaly_fires_loud_warning_when_tripped(capsys):
    budget = _BuildBudget(10)
    for _ in range(3):
        _report_gate_skip_anomaly(budget, "sast", "semgrep", True)
    out = capsys.readouterr().out
    assert "Gate anomaly suspected" in out
    assert "sast" in out


def test_qa_record_flags_block_with_empty_evidence(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    evidence_path = "evidence/add/A001/attempt_1/sast.txt"
    full = pcp_dir / evidence_path
    full.parent.mkdir(parents=True)
    full.write_text("")  # the phantom-block shape: a block with nothing behind it

    with patch("pcp.escalations.record") as mock_record:
        _qa_record(
            pcp_dir, CTX, "sast", ["SAST (semgrep) found issues — full list: ..."],
            control_id="CTRL-003", evidence_path=evidence_path,
        )
    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["route"] == "evidence-integrity-anomaly"
    assert "empty evidence" in kwargs["findings"][0] or "ungrounded" in kwargs["findings"][0]


def test_qa_record_does_not_flag_block_with_real_evidence(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    evidence_path = "evidence/add/A001/attempt_1/sast.txt"
    full = pcp_dir / evidence_path
    full.parent.mkdir(parents=True)
    full.write_text("real_finding.py:12: hardcoded secret")

    with patch("pcp.escalations.record") as mock_record:
        _qa_record(
            pcp_dir, CTX, "sast", ["SAST (semgrep) found issues — full list: ..."],
            control_id="CTRL-003", evidence_path=evidence_path,
        )
    mock_record.assert_not_called()


def test_qa_record_does_not_flag_a_pass(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.escalations.record") as mock_record:
        _qa_record(pcp_dir, CTX, "sast", [], control_id="CTRL-003", evidence_path=None)
    mock_record.assert_not_called()
