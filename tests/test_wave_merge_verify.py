from unittest.mock import patch

from pcp.commands.build import _run_wave_merge
from pcp import telemetry


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def _wave_module(name="widgets"):
    return {"name": name, "spec": {"dependencies": []}}


def test_wave_architect_review_drops_ungrounded_block_via_adversarial_verify(tmp_path):
    """Same adversarial re-verification per-criterion architect-review already
    gets (_verify_block_findings) -- previously wave-level BLOCK findings went
    straight from one Haiku call into a blocked wave-merge with no second
    opinion at all."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    review_response = {"findings": [
        {"severity": "BLOCK", "location": "real.py", "finding": "real cross-module coupling", "principle": "p", "fix": "f"},
        {"severity": "BLOCK", "location": "fake.py", "finding": "hallucinated issue", "principle": "p", "fix": "f"},
    ]}
    verify_response = {"verdicts": [
        {"index": 0, "refuted": False, "reason": "grounded in the diff"},
        {"index": 1, "refuted": True, "reason": "fake.py not touched by this wave"},
    ]}

    with patch("pcp.qa.run_test_suite", return_value={"tool": None, "passed": True, "output": ""}), \
         patch("pcp.commands.build.llm.call_json",
               side_effect=[
                   review_response,                        # wave architect-review call (return_meta=False)
                   (verify_response, {"model": "haiku"}),   # adversarial verify call (return_meta=True)
               ]), \
         patch("pcp.commands.architect_review._get_diff", return_value="diff --git a/real.py\n+something"), \
         patch("pcp.commands.architect_review._load_persona", return_value="persona"), \
         patch("pcp.commands.architect_review._load_kb", return_value=""):
        findings = _run_wave_merge(pcp_dir, [_wave_module()], "HEAD~1", wave_number=0)

    assert any("real cross-module coupling" in f for f in findings)
    assert not any("hallucinated issue" in f for f in findings)

    records = _qa_records(pcp_dir)
    checks = [r["check"] for r in records]
    assert "wave-architect-review-verify" in checks
    final = [r for r in records if r["check"] == "wave-architect-review"][-1]
    assert final["error_count"] == 1
    assert final["module"] is None  # _wave_record's own convention, unchanged
