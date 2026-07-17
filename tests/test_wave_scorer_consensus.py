"""Wave-gate scorer-consensus rule (dogfood round 3): deterministic assertion
coverage disagreeing with a green LLM score must not hard-block the wave."""

from unittest.mock import patch

from pcp.commands.build import _run_wave_merge


def _pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy" / "modules").mkdir(parents=True)
    return pcp_dir


def _run(tmp_path, vs_result):
    pcp_dir = _pcp(tmp_path)
    with patch("pcp.commands.validate_strategy.run_validate_strategy", return_value=vs_result), \
         patch("pcp.qa.run_test_suite", return_value={"tool": None, "passed": True, "output": ""}), \
         patch("pcp.commands.architect_review._get_diff", return_value=""):
        return _run_wave_merge(pcp_dir, [], "HEAD", wave_number=0)


def test_scorer_disagreement_does_not_block(tmp_path):
    vs = {
        "coverage_score": 0.5, "llm_coverage_score": 1.0,
        "scoring_method": "deterministic",
        "coverage_gaps": [{"area": "cli"}, {"area": "perf"}],
        "coupling_violations": [], "coupling_score": 0.6,
    }
    findings = _run(tmp_path, vs)
    assert findings == []


def test_scorer_consensus_still_blocks(tmp_path):
    vs = {
        "coverage_score": 0.5, "llm_coverage_score": 0.5,
        "scoring_method": "deterministic",
        "coverage_gaps": [{"area": "cli"}],
        "coupling_violations": [], "coupling_score": 0.9,
    }
    findings = _run(tmp_path, vs)
    assert any("validate-strategy" in f for f in findings)


def test_severe_coupling_blocks_even_with_disagreement(tmp_path):
    vs = {
        "coverage_score": 0.5, "llm_coverage_score": 1.0,
        "scoring_method": "deterministic",
        "coverage_gaps": [{"area": "cli"}],
        "coupling_violations": [{"type": "circular", "modules": ["a", "b"]}],
        "coupling_score": 0.4,
    }
    findings = _run(tmp_path, vs)
    assert any("severe coupling" in f for f in findings)


def test_llm_only_scoring_unchanged(tmp_path):
    vs = {
        "coverage_score": 0.5, "scoring_method": "llm",
        "coverage_gaps": [{"area": "cli"}],
        "coupling_violations": [], "coupling_score": 0.9,
    }
    findings = _run(tmp_path, vs)
    assert any("validate-strategy" in f for f in findings)
