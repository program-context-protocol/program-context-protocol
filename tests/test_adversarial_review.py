"""CTRL-041, adversarial review (2026-08-08) -- an opt-in per-criterion check
that spawns a SEPARATE real coding-agent session whose only job is to try to
disprove the primary build agent's own self-report, before a criterion is
marked complete. See build.py's _run_adversarial_review docstring."""

import json
from unittest.mock import MagicMock, patch

from pcp.commands.build import (
    _run_adversarial_review, _build_adversarial_review_prompt, _BuildBudget, BudgetExceeded,
)
from pcp import telemetry


def _envelope(result_obj, session_id="rev-1", cost=0.42):
    return json.dumps({
        "is_error": False, "result": json.dumps(result_obj), "session_id": session_id,
        "usage": {"input_tokens": 10, "output_tokens": 20}, "total_cost_usd": cost,
    })


def _mod():
    return {"name": "scoring-engine"}


def _crit(adversarial_review=True):
    return {"id": "A001", "description": "Fraud score computed from real transaction features.",
            "adversarial_review": adversarial_review}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("check") == "adversarial-review"]


def test_noop_when_criterion_did_not_opt_in(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)
    findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(adversarial_review=False), "diff content", budget)
    assert findings == []


def test_noop_when_diff_is_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)
    findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "   ", budget)
    assert findings == []


def test_budget_circuit_breaker_skips_gracefully(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(0)  # already exhausted
    with patch("pcp.commands.build.subprocess.Popen") as mock_popen:
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "some diff", budget)
    assert findings == []
    mock_popen.assert_not_called()


def test_real_implementation_produces_no_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self._cmd = cmd
            self.returncode = 0
            self.pid = 1
        def communicate(self, input=None, timeout=None):
            return _envelope({"is_real": True, "confidence": 0.9, "red_flags": [], "reasoning": "Real logic, real assertions."}), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert findings == []
    records = _qa_records(pcp_dir)
    assert len(records) == 1
    assert records[0]["result"] == "pass"
    assert budget.run_cost_total == 0.42


def test_fake_implementation_produces_a_blocking_finding(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            return _envelope({
                "is_real": False, "confidence": 0.85,
                "red_flags": ["test_score.py asserts mock.called, never checks the real score value"],
                "reasoning": "The test mocks out the scoring function entirely; nothing exercises real logic.",
            }), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert len(findings) == 1
    assert "could NOT confirm" in findings[0]
    assert "mock.called" in findings[0]
    records = _qa_records(pcp_dir)
    assert records[0]["result"] == "block"


def test_timeout_fails_open_not_a_finding(tmp_path):
    import subprocess
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 1
            self._calls = 0
        def communicate(self, input=None, timeout=None):
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=900)
            return "", ""  # the reap call after killpg -- real Popen returns, doesn't re-raise

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen), \
         patch("pcp.commands.build.os.killpg"), patch("pcp.commands.build.os.getpgid", return_value=1):
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert findings == []  # infra failure, not a real finding -- fails open
    records = _qa_records(pcp_dir)
    assert records[0]["result"] == "error"


def test_nonzero_exit_fails_open(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 1
        def communicate(self, input=None, timeout=None):
            return "", "crashed"

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert findings == []
    assert _qa_records(pcp_dir)[0]["result"] == "error"


def test_unparseable_verdict_fails_open(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            return "not valid json at all", ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_adversarial_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert findings == []
    assert _qa_records(pcp_dir)[0]["result"] == "error"


def test_prompt_instructs_no_edits_and_includes_the_diff():
    prompt = _build_adversarial_review_prompt("scoring-engine", _crit(), "diff --git a/x.py b/x.py\n+real code")
    assert "MUST NOT edit" in prompt
    assert "diff --git a/x.py" in prompt
    assert "scoring-engine" in prompt
    assert "A001" in prompt


def test_prompt_truncates_a_very_large_diff():
    huge_diff = "x" * 50000
    prompt = _build_adversarial_review_prompt("scoring-engine", _crit(), huge_diff)
    assert len(prompt) < 20000
