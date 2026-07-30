"""A killed test run must still say what it was doing.

Measured on ontology-foundry 2026-07-30: a 900s pytest timeout wrote a 311-byte
evidence file containing only PCP's own advice message. 12 of that run's 19
test-gate blocks were timeouts, so 12 full retries were spent and nobody could
tell which test hung. TimeoutExpired carries the partial output; every handler
discarded it.
"""
import subprocess
from unittest.mock import patch

from pcp import qa


def _timeout(stdout=None, stderr=None):
    return subprocess.TimeoutExpired(cmd=["pytest"], timeout=900, output=stdout, stderr=stderr)


def test_partial_stdout_is_carried_into_the_message():
    exc = _timeout(stdout="tests/core/test_db.py::test_pool_acquire ")
    msg = qa._timeout_message("pytest", exc)
    assert "PCP_QA_TEST_TIMEOUT_SEC" in msg                # advice preserved
    assert "test_pool_acquire" in msg                      # and now the data
    assert "partial stdout" in msg


def test_partial_stderr_is_carried_too():
    msg = qa._timeout_message("pytest", _timeout(stderr="connection to server at 5432 failed"))
    assert "connection to server at 5432 failed" in msg
    assert "partial stderr" in msg


def test_bytes_streams_are_decoded_not_repr_dumped():
    msg = qa._timeout_message("pytest", _timeout(stdout=b"test_hang \xff started"))
    assert "test_hang" in msg
    assert "\\xff" not in msg


def test_silence_before_the_kill_is_reported_as_its_own_signal():
    """No output is itself diagnostic — it means the hang preceded collection."""
    msg = qa._timeout_message("pytest", _timeout())
    assert "no partial output was captured" in msg
    assert "import/fixture time" in msg


def test_message_without_an_exception_is_unchanged():
    """Callers that have no exception object still get the original prose."""
    msg = qa._timeout_message("pytest")
    assert "was killed" in msg
    assert "partial" not in msg


def test_long_output_is_tail_truncated_not_head_truncated():
    """The end is where the hang is; keep the tail."""
    msg = qa._timeout_message("pytest", _timeout(stdout="X" * 9000 + "LAST_TEST_STARTED"))
    assert "LAST_TEST_STARTED" in msg
    assert len(msg) < 6000


def test_reports_the_limit_that_actually_applied_not_the_current_env():
    """test_timeout_info() reports the limit in effect NOW; exc.timeout is the
    one that killed this process. When they disagree, the exception wins."""
    msg = qa._timeout_message("pytest", _timeout())
    assert "900s PCP_QA_TEST_TIMEOUT_SEC" in msg
    assert "300s" not in msg
    # 900 is not the 300s default, so it must not be labelled as one
    assert "PCP default" not in msg


def test_pytest_runner_carries_partial_output_through_to_its_result():
    from pathlib import Path
    with patch.object(qa, "project_tool", return_value="/fake/pytest"), \
         patch.object(qa, "testmon_available", return_value=False), \
         patch("subprocess.run", side_effect=_timeout(stdout="collected 1246 items")):
        out = qa._run_pytest(Path("."))
    assert out["passed"] is False
    assert "collected 1246 items" in out["output"]
