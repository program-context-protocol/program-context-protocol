from unittest.mock import patch

from pcp.commands.build import (
    _run_test_suite_check, _run_lint_check, _run_sast_check,
    _run_architect_review, _run_gate_check, _BuildBudget,
)
from pcp import telemetry

CTX = {"attempt": 1, "module": "add", "criterion_id": "A001"}


def _last_qa_record(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"][-1]


def test_test_suite_check_passes_pcp_dir_and_changed_files_through(tmp_path):
    """impact.py's scoping (see test_qa.py/test_impact.py) needs both
    pcp_dir and this attempt's changed files -- verify the wiring, not the
    scoping logic itself (covered elsewhere)."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    ctx = {**CTX, "files": ["src/x.py"]}
    with patch("pcp.commands.build.qa.run_test_suite",
               return_value={"tool": "pytest", "passed": True, "output": "ok"}) as mock_run:
        _run_test_suite_check(pcp_dir, tmp_path, ctx)
    _, kwargs = mock_run.call_args
    assert kwargs["pcp_dir"] == pcp_dir
    assert kwargs["changed_files"] == ["src/x.py"]


def test_test_suite_check_prints_scoped_note_when_scoped(tmp_path, capsys):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.qa.run_test_suite",
               return_value={"tool": "pytest", "passed": True, "output": "ok", "scoped_to": ["tests/test_x.py"]}):
        _run_test_suite_check(pcp_dir, tmp_path, CTX)
    out = capsys.readouterr().out
    assert "scoped to impacted modules" in out
    assert "tests/test_x.py" in out


def test_test_suite_check_stores_full_untruncated_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    long_output = "line\n" * 2000  # far past qa.py's 3000-char truncation window
    with patch("pcp.commands.build.qa.run_test_suite",
               return_value={"tool": "pytest", "passed": False, "output": long_output}):
        violations = _run_test_suite_check(pcp_dir, tmp_path, CTX)

    assert violations  # still blocks as before
    rec = _last_qa_record(pcp_dir)
    assert rec["evidence_path"] is not None
    stored = (pcp_dir / rec["evidence_path"]).read_text()
    assert stored == long_output  # full, not the 1500-char console snippet


def test_test_suite_check_stores_evidence_on_pass_too(tmp_path):
    """Real gap this closes: a PASS previously recorded nothing beyond the
    word 'pass' -- now the actual output is on disk even when nothing failed."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.qa.run_test_suite",
               return_value={"tool": "pytest", "passed": True, "output": "5 passed in 0.02s"}):
        _run_test_suite_check(pcp_dir, tmp_path, CTX)

    rec = _last_qa_record(pcp_dir)
    assert rec["result"] == "pass"
    assert rec["evidence_path"] is not None
    assert (pcp_dir / rec["evidence_path"]).read_text() == "5 passed in 0.02s"


def test_test_suite_check_no_evidence_when_tool_not_detected(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.qa.run_test_suite",
               return_value={"tool": None, "passed": True, "output": ""}):
        _run_test_suite_check(pcp_dir, tmp_path, CTX)
    rec = _last_qa_record(pcp_dir)
    assert rec["result"] == "skipped"
    assert rec["evidence_path"] is None


def test_lint_check_stores_full_issue_list_beyond_console_cap(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    issues = [f"file.py:{i}: some issue" for i in range(50)]  # past the 10-issue console cap
    with patch("pcp.commands.build.qa.run_lint",
               return_value={"tool": "ruff", "passed": False, "issues": issues}):
        _run_lint_check(pcp_dir, tmp_path, ["file.py"], CTX, _BuildBudget(10))

    rec = _last_qa_record(pcp_dir)
    stored = (pcp_dir / rec["evidence_path"]).read_text()
    assert stored.count("some issue") == 50


def test_sast_check_stores_full_findings_beyond_console_cap(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    findings = [f"finding {i}" for i in range(30)]
    with patch("pcp.commands.build.qa.run_sast",
               return_value={"tool": "semgrep", "passed": False, "findings": findings}):
        _run_sast_check(pcp_dir, tmp_path, ["file.py"], CTX, _BuildBudget(10))

    rec = _last_qa_record(pcp_dir)
    stored = (pcp_dir / rec["evidence_path"]).read_text()
    assert stored.count("finding") == 30


def test_architect_review_stores_full_judge_response(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "architect_persona.md").write_text("principles")
    judge_response = {
        "findings": [{"severity": "BLOCK", "location": "x", "finding": "y", "principle": "p", "fix": "f"}]
        + [{"severity": "info", "location": f"loc{i}", "finding": "not a block"} for i in range(20)],
    }
    with patch("pcp.commands.build.llm.call_json", return_value=(judge_response, {"model": "haiku"})), \
         patch("pcp.commands.architect_review._load_persona", return_value="p"), \
         patch("pcp.commands.architect_review._load_kb", return_value=""):
        blocks = _run_architect_review(pcp_dir, "some diff", ["file.py"], CTX)

    assert len(blocks) == 1  # only the BLOCK surfaces as a violation
    rec = _last_qa_record(pcp_dir)
    import json
    stored = json.loads((pcp_dir / rec["evidence_path"]).read_text())
    assert len(stored["findings"]) == 21  # but the FULL judge response (all 21) is on disk


def test_gate_check_stores_full_judge_response(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    judge_response = {
        "recommendation": "block", "alignment_score": 0.1, "summary": "drifted",
        "regressions": ["r1", "r2"], "llm_rule_violations": ["v1"],
    }
    with patch("pcp.commands.build.llm.call_json", return_value=(judge_response, {"model": "haiku"})), \
         patch("pcp.commands.gate._load_llm_rules", return_value=[]):
        issues = _run_gate_check(pcp_dir, "some diff", CTX)

    assert issues  # blocked as expected
    rec = _last_qa_record(pcp_dir)
    import json
    stored = json.loads((pcp_dir / rec["evidence_path"]).read_text())
    assert stored["summary"] == "drifted"
    assert stored["regressions"] == ["r1", "r2"]
