"""Proactive flaky-test detection (2026-08-08) -- facet 2 of the testing
prior-art sweep. Real-execution-based: reruns the suite N times and diffs
per-test outcomes, unlike test_composition.py/falsegreen which are static."""

import shutil
import textwrap
from unittest.mock import patch, MagicMock

import pytest

from pcp.flaky_detect import (
    pytest_randomly_available, _parse_junit_outcomes, run_flaky_detection,
    flaky_detect_available,
)

HAS_PYTEST = shutil.which("pytest") is not None


# ── pytest_randomly_available: mocked subprocess ──

def test_randomly_detected_when_present_in_plugin_list():
    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"), \
         patch("pcp.flaky_detect.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout="pytest 8.0.0\nplugins: randomly-3.15.0, cov-4.1.0\n", stderr="",
        )
        assert pytest_randomly_available("dummy") is True


def test_randomly_absent_when_not_in_plugin_list():
    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"), \
         patch("pcp.flaky_detect.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="pytest 8.0.0\nplugins: cov-4.1.0\n", stderr="")
        assert pytest_randomly_available("dummy") is False


def test_randomly_false_when_no_pytest_at_all():
    with patch("pcp.flaky_detect.project_tool", return_value=None):
        assert pytest_randomly_available("dummy") is False


# ── _parse_junit_outcomes: pure XML parsing ──

def test_parses_passed_failed_error_skipped(tmp_path):
    xml = tmp_path / "run.xml"
    xml.write_text(textwrap.dedent("""\
        <testsuite>
          <testcase classname="test_a" name="test_pass"></testcase>
          <testcase classname="test_a" name="test_fail"><failure message="x"/></testcase>
          <testcase classname="test_a" name="test_error"><error message="x"/></testcase>
          <testcase classname="test_a" name="test_skip"><skipped/></testcase>
        </testsuite>
    """))
    outcomes = _parse_junit_outcomes(xml)
    assert outcomes["test_a::test_pass"] == "passed"
    assert outcomes["test_a::test_fail"] == "failed"
    assert outcomes["test_a::test_error"] == "error"
    assert outcomes["test_a::test_skip"] == "skipped"


def test_missing_xml_fails_open_to_empty_dict(tmp_path):
    assert _parse_junit_outcomes(tmp_path / "nope.xml") == {}


def test_malformed_xml_fails_open_to_empty_dict(tmp_path):
    xml = tmp_path / "bad.xml"
    xml.write_text("<not valid xml")
    assert _parse_junit_outcomes(xml) == {}


# ── flaky_detect_available ──

def test_flaky_detect_available_reflects_pytest_presence():
    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"):
        assert flaky_detect_available("dummy") is True
    with patch("pcp.flaky_detect.project_tool", return_value=None):
        assert flaky_detect_available("dummy") is False


# ── run_flaky_detection: mocked subprocess, real XML writes ──

def test_unavailable_when_no_pytest(tmp_path):
    with patch("pcp.flaky_detect.project_tool", return_value=None):
        result = run_flaky_detection(tmp_path, runs=3)
    assert result == {"available": False}


def test_identifies_a_test_that_flips_across_runs(tmp_path):
    """test_flip alternates outcome per run; test_stable always passes."""
    fixtures = [
        {"test_a::test_flip": "passed", "test_a::test_stable": "passed"},
        {"test_a::test_flip": "failed", "test_a::test_stable": "passed"},
        {"test_a::test_flip": "passed", "test_a::test_stable": "passed"},
    ]
    call_count = {"n": 0}

    def fake_run(args, **kwargs):
        xml_arg = next(a for a in args if a.startswith("--junit-xml="))
        xml_path = xml_arg.split("=", 1)[1]
        outcomes = fixtures[call_count["n"]]
        call_count["n"] += 1
        tag_for = {"passed": None, "failed": "failure", "error": "error", "skipped": "skipped"}
        cases = "".join(
            f'<testcase classname="{tid.split("::")[0]}" name="{tid.split("::")[1]}">'
            + ("" if tag_for[outc] is None else f"<{tag_for[outc]}/>")
            + "</testcase>"
            for tid, outc in outcomes.items()
        )
        with open(xml_path, "w") as f:
            f.write(f"<testsuite>{cases}</testsuite>")
        return MagicMock(returncode=1, stdout="", stderr="")

    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"), \
         patch("pcp.flaky_detect.pytest_randomly_available", return_value=False), \
         patch("pcp.flaky_detect.subprocess.run", side_effect=fake_run):
        result = run_flaky_detection(tmp_path, runs=3)

    assert result["available"] is True
    assert result["runs"] == 3
    assert result["total_unique_tests"] == 2
    flaky_ids = {ft["test_id"] for ft in result["flaky_tests"]}
    assert flaky_ids == {"test_a::test_flip"}


def test_consistent_pass_or_fail_is_not_flaky(tmp_path):
    def fake_run(args, **kwargs):
        xml_arg = next(a for a in args if a.startswith("--junit-xml="))
        xml_path = xml_arg.split("=", 1)[1]
        with open(xml_path, "w") as f:
            f.write('<testsuite><testcase classname="test_a" name="test_stable"></testcase></testsuite>')
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"), \
         patch("pcp.flaky_detect.pytest_randomly_available", return_value=False), \
         patch("pcp.flaky_detect.subprocess.run", side_effect=fake_run):
        result = run_flaky_detection(tmp_path, runs=3)

    assert result["flaky_tests"] == []


def test_timeout_is_recorded_not_raised(tmp_path):
    import subprocess as sp

    def fake_run(args, **kwargs):
        raise sp.TimeoutExpired(cmd=args, timeout=1)

    with patch("pcp.flaky_detect.project_tool", return_value="/usr/bin/pytest"), \
         patch("pcp.flaky_detect.pytest_randomly_available", return_value=False), \
         patch("pcp.flaky_detect.subprocess.run", side_effect=fake_run):
        result = run_flaky_detection(tmp_path, runs=2)

    assert result["available"] is True
    assert result["any_run_timed_out"] is True
    assert result["flaky_tests"] == []


# ── real end-to-end, no mocks ──

@pytest.mark.skipif(not HAS_PYTEST, reason="pytest not on PATH")
def test_real_pytest_catches_a_genuinely_flaky_test(tmp_path):
    """A test whose outcome depends on a counter file flips deterministically
    on alternating runs -- reproduces real order-independent state-leak
    flakiness without relying on actual randomness (keeps the test itself
    non-flaky)."""
    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "test_flip.py").write_text(textwrap.dedent("""
        import pathlib
        def test_alternates():
            counter_path = pathlib.Path(__file__).parent / "counter.txt"
            n = int(counter_path.read_text()) if counter_path.exists() else 0
            counter_path.write_text(str(n + 1))
            assert n % 2 == 0

        def test_always_passes():
            assert True
    """))
    result = run_flaky_detection(tmp_path, runs=3, timeout_sec=60.0)
    assert result["available"] is True
    flaky_ids = {ft["test_id"] for ft in result["flaky_tests"]}
    assert "test_flip::test_alternates" in flaky_ids
    assert "test_flip::test_always_passes" not in flaky_ids
