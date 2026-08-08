"""Wave-merge test-suite baseline exclusion (Project W dogfood, 2026-08-08).

Bug report: a pre-existing bug anywhere in the unscoped wave-merge test run
used to block every unrelated criterion in every unrelated module, forever
-- the wave-merge test-suite step is unscoped BY DESIGN (see qa.run_test_suite's
docstring), so a genuinely irrelevant pre-existing failure had no way to be
excluded. Mirrors ci_rules.yaml's existing baseline_violations.yaml
brownfield-grace pattern (check.py --baseline), extended to the test suite.
"""

from unittest.mock import patch

from pcp import telemetry
from pcp.commands.build import _run_wave_merge


def _pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    (pcp_dir / "strategy" / "modules").mkdir(parents=True)
    return pcp_dir


def _run(tmp_path, test_result, baseline):
    pcp_dir = _pcp(tmp_path)
    with patch("pcp.commands.validate_strategy.run_validate_strategy", return_value=None), \
         patch("pcp.qa.run_test_suite", return_value=test_result), \
         patch("pcp.qa.load_baseline_test_failures", return_value=baseline), \
         patch("pcp.commands.architect_review._get_diff", return_value=""):
        findings = _run_wave_merge(pcp_dir, [], "HEAD", wave_number=0)
    return pcp_dir, findings


def test_all_failures_baselined_does_not_block_the_wave(tmp_path):
    test_result = {
        "tool": "pytest", "passed": False, "output": "2 failed",
        "failed_test_ids": ["tests/test_a.py::test_x", "tests/test_b.py::test_y"],
    }
    pcp_dir, findings = _run(tmp_path, test_result, {"tests/test_a.py::test_x", "tests/test_b.py::test_y"})

    assert not any("Wave integration suite" in f for f in findings)
    rec = [r for r in telemetry.load(pcp_dir) if r.get("check") == "wave-test-suite"][-1]
    assert rec["result"] == "advisory"
    assert rec["errors"]  # the exclusion is recorded, not silently dropped


def test_a_new_failure_alongside_baselined_ones_still_blocks_the_wave(tmp_path):
    test_result = {
        "tool": "pytest", "passed": False, "output": "2 failed",
        "failed_test_ids": ["tests/test_a.py::test_x", "tests/test_new.py::test_regression"],
    }
    pcp_dir, findings = _run(tmp_path, test_result, {"tests/test_a.py::test_x"})

    assert any("Wave integration suite" in f for f in findings)
    assert any("NOT in the baseline" in f for f in findings)
    rec = [r for r in telemetry.load(pcp_dir) if r.get("check") == "wave-test-suite"][-1]
    assert rec["result"] == "block"


def test_no_baseline_captured_blocks_as_before(tmp_path):
    test_result = {
        "tool": "pytest", "passed": False, "output": "1 failed",
        "failed_test_ids": ["tests/test_a.py::test_x"],
    }
    pcp_dir, findings = _run(tmp_path, test_result, set())
    assert any("Wave integration suite" in f for f in findings)
