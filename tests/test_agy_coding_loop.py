"""AgyCodingAgentHarness -- the (agy-drafted, human-reviewed) second
implementation of CodingAgentHarness. See harness/agy_coding_loop.py's
module docstring for provenance and honest limitations. This is Stage 3
of the multi-harness plan for agy specifically -- NOT wired into `pcp
build` (that's a separate, deliberate decision, not made here)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from pcp.llm.coding_agent_contract import CodingAgentRequest
from pcp.llm.harness.agy_coding_loop import AgyCodingAgentHarness


def _request(**overrides) -> CodingAgentRequest:
    base = dict(
        prompt="implement the thing", cwd=Path("/tmp/worktree"), session_id="req-session-1",
        resume_session_id=None, model=None, timeout_sec=300, max_budget_usd="5.00",
    )
    base.update(overrides)
    return CodingAgentRequest(**base)


def _envelope(status="SUCCESS", conversation_id="conv-1", total_tokens=1000, response="did it"):
    return json.dumps({
        "conversation_id": conversation_id, "status": status, "response": response,
        "duration_seconds": 2.0,
        "usage": {"input_tokens": total_tokens - 10, "output_tokens": 10, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": total_tokens},
    })


def test_fresh_session_does_not_pass_conversation_flag():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        harness.run(_request(resume_session_id=None))
    cmd = mock_run.call_args.kwargs.get("args") or mock_run.call_args.args[0]
    assert "--conversation" not in cmd


def test_resume_passes_conversation_flag_with_the_resume_id():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        harness.run(_request(resume_session_id="prior-conv-id"))
    cmd = mock_run.call_args.args[0]
    idx = cmd.index("--conversation")
    assert cmd[idx + 1] == "prior-conv-id"


def test_effort_not_passed_by_default_unlike_the_verifier_leg():
    """The one real bug fixed from agy's own first draft -- it copied
    --effort low from harness/agy.py's verifier leg without noticing code
    generation isn't a cheap yes/no judgment call."""
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        harness.run(_request())
    cmd = mock_run.call_args.args[0]
    assert "--effort" not in cmd


def test_effort_passed_when_env_override_set(monkeypatch):
    monkeypatch.setenv("PCP_AGY_CODING_EFFORT", "high")
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        harness.run(_request())
    cmd = mock_run.call_args.args[0]
    idx = cmd.index("--effort")
    assert cmd[idx + 1] == "high"


def test_passes_unattended_editing_flags_scoped_to_cwd():
    harness = AgyCodingAgentHarness()
    cwd = Path("/tmp/some-worktree")
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        harness.run(_request(cwd=cwd))
    cmd = mock_run.call_args.args[0]
    assert "--dangerously-skip-permissions" in cmd
    assert "--mode" in cmd and "accept-edits" in cmd
    assert "--sandbox" in cmd
    idx = cmd.index("--add-dir")
    assert cmd[idx + 1] == str(cwd)
    assert mock_run.call_args.kwargs["cwd"] == cwd


def test_timeout_passed_to_subprocess_and_reflected_on_expiry():
    import subprocess as _subprocess
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run", side_effect=_subprocess.TimeoutExpired(cmd="agy", timeout=42)):
        result = harness.run(_request(timeout_sec=42))
    assert result.ok is False
    assert "42" in result.error


def test_successful_call_returns_real_usage_and_session_id():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(conversation_id="conv-real", total_tokens=777), stderr="")
        result = harness.run(_request())
    assert result.ok is True
    assert result.session_id == "conv-real"
    assert result.usage["total_tokens"] == 777
    assert result.cost_usd is None


def test_non_success_status_is_a_failed_attempt():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(status="FAILURE"), stderr="")
        result = harness.run(_request())
    assert result.ok is False


def test_nonzero_exit_is_a_failed_attempt():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        result = harness.run(_request())
    assert result.ok is False
    assert "boom" in result.error


def test_malformed_json_is_a_failed_attempt_not_a_crash():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        result = harness.run(_request())
    assert result.ok is False
    assert "JSON" in result.error


def test_token_ceiling_exceeded_is_a_failed_attempt_not_silently_accepted():
    harness = AgyCodingAgentHarness(max_tokens_limit=100)
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(total_tokens=1000), stderr="")
        result = harness.run(_request())
    assert result.ok is False
    assert "1000" in result.error and "100" in result.error
    # Real usage still reported even on a fail-safe reject -- not discarded.
    assert result.usage["total_tokens"] == 1000


def test_default_token_ceiling_reads_from_env(monkeypatch):
    monkeypatch.setenv("PCP_AGY_MAX_TOKENS_PER_CALL", "50")
    harness = AgyCodingAgentHarness()
    assert harness.max_tokens_limit == 50


def test_missing_binary_is_a_failed_attempt():
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run", side_effect=FileNotFoundError()):
        result = harness.run(_request())
    assert result.ok is False
    assert "not found" in result.error.lower()


def test_never_reports_changed_files_or_diff():
    """Contract policy point 3 -- CodingAgentResult has no such fields at
    all, so this is really a schema check, but worth asserting explicitly
    since it's the property a naive re-implementation is most likely to
    add "for convenience"."""
    harness = AgyCodingAgentHarness()
    with patch("pcp.llm.harness.agy_coding_loop.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope(), stderr="")
        result = harness.run(_request())
    assert not hasattr(result, "diff")
    assert not hasattr(result, "changed_files")
