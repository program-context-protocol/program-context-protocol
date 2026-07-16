import json
from unittest.mock import patch

from pcp.commands.build import (
    _verify_block_findings, _run_architect_review, _run_gate_check,
)
from pcp import telemetry

CTX = {"attempt": 1, "module": "add", "criterion_id": "A001"}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def _last_qa_record(pcp_dir):
    return _qa_records(pcp_dir)[-1]


# ── _verify_block_findings, direct unit tests ──

def test_empty_findings_skip_verification_entirely(tmp_path):
    """No wasted call, no telemetry record, when there's nothing to verify --
    matches the 'don't verify what doesn't exist' cost discipline."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        kept, dropped = _verify_block_findings(pcp_dir, "diff", [], CTX, "architect-review", "CTRL-005")
    assert kept == []
    assert dropped == []
    mock_call.assert_not_called()


def test_verifier_drops_refuted_finding_keeps_grounded_one(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    verdicts = {"verdicts": [
        {"index": 0, "refuted": False, "reason": "matches the diff"},
        {"index": 1, "refuted": True, "reason": "no such file in the diff"},
    ]}
    with patch("pcp.commands.build.llm.call_json", return_value=(verdicts, {"model": "haiku"})):
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff", ["real finding", "hallucinated finding"], CTX, "architect-review", "CTRL-005",
        )
    assert kept == ["real finding"]
    assert len(dropped) == 1
    assert "hallucinated finding" in dropped[0]
    assert "no such file in the diff" in dropped[0]


def test_verifier_call_batched_once_regardless_of_finding_count(tmp_path):
    """Token discipline: one extra call per check, not one per finding."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    verdicts = {"verdicts": [{"index": i, "refuted": False, "reason": ""} for i in range(5)]}
    with patch("pcp.commands.build.llm.call_json", return_value=(verdicts, {})) as mock_call:
        kept, _dropped = _verify_block_findings(
            pcp_dir, "diff", [f"finding {i}" for i in range(5)], CTX, "gate", "CTRL-006",
        )
    assert mock_call.call_count == 1
    assert len(kept) == 5


def test_verifier_fails_open_on_exception_keeps_all_findings(tmp_path):
    """A broken verifier must never silently swallow a real BLOCK -- keep
    everything unchanged and record the failure as an error, not a pass."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.llm.call_json", side_effect=RuntimeError("timeout")):
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff", ["real finding"], CTX, "architect-review", "CTRL-005",
        )
    assert kept == ["real finding"]
    assert dropped == []
    rec = _last_qa_record(pcp_dir)
    assert rec["check"] == "architect-review-verify"
    assert rec["result"] == "error"


def test_verifier_defaults_to_keeping_finding_with_no_matching_verdict(tmp_path):
    """If the judge's response is malformed/missing an index, fail open for
    that specific finding rather than silently dropping it."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.build.llm.call_json", return_value=({"verdicts": []}, {})):
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff", ["finding with no verdict returned"], CTX, "gate", "CTRL-006",
        )
    assert kept == ["finding with no verdict returned"]
    assert dropped == []


# ── end-to-end through the real gate call sites ──

def test_architect_review_end_to_end_drops_ungrounded_block(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "architect_persona.md").write_text("principles")
    review_response = {
        "findings": [
            {"severity": "BLOCK", "location": "real.py", "finding": "real issue", "principle": "p", "fix": "f"},
            {"severity": "BLOCK", "location": "fake.py", "finding": "hallucinated issue", "principle": "p", "fix": "f"},
        ],
    }
    verify_response = {"verdicts": [
        {"index": 0, "refuted": False, "reason": "grounded"},
        {"index": 1, "refuted": True, "reason": "fake.py not touched by this diff"},
    ]}
    with patch("pcp.commands.build.llm.call_json",
               side_effect=[(review_response, {"model": "haiku"}), (verify_response, {"model": "haiku"})]), \
         patch("pcp.commands.architect_review._load_persona", return_value="p"), \
         patch("pcp.commands.architect_review._load_kb", return_value=""):
        blocks = _run_architect_review(pcp_dir, "some diff", ["real.py"], CTX)

    assert len(blocks) == 1
    assert "real issue" in blocks[0]

    records = _qa_records(pcp_dir)
    checks = [r["check"] for r in records]
    assert "architect-review-verify" in checks
    assert "architect-review" in checks
    # Final architect-review record reflects the POST-verification finding count.
    final = [r for r in records if r["check"] == "architect-review"][-1]
    assert final["error_count"] == 1


def test_gate_check_end_to_end_drops_ungrounded_regression(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    gate_response = {
        "recommendation": "block", "alignment_score": 0.1, "summary": "drifted",
        "regressions": ["real regression"], "llm_rule_violations": ["hallucinated violation"],
    }
    verify_response = {"verdicts": [
        {"index": 0, "refuted": False, "reason": "grounded in the score"},
        {"index": 1, "refuted": False, "reason": "grounded"},
        {"index": 2, "refuted": True, "reason": "rule not actually referenced anywhere"},
    ]}
    with patch("pcp.commands.build.llm.call_json",
               side_effect=[(gate_response, {"model": "haiku"}), (verify_response, {"model": "haiku"})]), \
         patch("pcp.commands.gate._load_llm_rules", return_value=[]):
        issues = _run_gate_check(pcp_dir, "some diff", CTX)

    assert any("real regression" in i for i in issues)
    assert not any("hallucinated violation" in i for i in issues)
    assert len(issues) == 2  # summary line + the one surviving regression
