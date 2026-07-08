import json
from unittest.mock import patch, MagicMock

from pcp.llm import client as llm


def _envelope(result="{}"):
    return json.dumps({
        "is_error": False, "result": result, "session_id": "s1",
        "usage": {"input_tokens": 1, "output_tokens": 1}, "total_cost_usd": 0.0,
        "duration_ms": 1,
    })


def test_call_passes_cwd_derived_from_pcp_dir(tmp_path):
    """Regression: call() never passed cwd to subprocess.run, so the `claude`
    subprocess always ran in whatever the CALLING PROCESS's actual OS cwd
    happened to be -- not necessarily the target project. Found via a real
    contamination incident: a test process's own cwd (this repo) leaked
    into a spawned agent invocation that should have run against an
    isolated test project instead."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user", pcp_dir=pcp_dir)
    assert mock_run.call_args.kwargs["cwd"] == pcp_dir.parent


def test_call_cwd_none_when_no_pcp_dir_given():
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user")
    assert mock_run.call_args.kwargs["cwd"] is None


def test_call_json_also_passes_cwd(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope('{"a": 1}'))
        result = llm.call_json("system", "user", pcp_dir=pcp_dir)
    assert mock_run.call_args.kwargs["cwd"] == pcp_dir.parent
    assert result == {"a": 1}


def test_call_raises_on_missing_claude_binary(tmp_path):
    with patch("pcp.llm.client.subprocess.run", side_effect=FileNotFoundError):
        try:
            llm.call("system", "user")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "claude CLI not found" in str(e)


def test_call_logs_usage_to_correct_pcp_dir(tmp_path):
    """The cwd fix and the token-ledger logging both derive from the same
    pcp_dir -- confirms _log_usage still writes to the real target project,
    not wherever cwd ended up pointing."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_envelope())
        llm.call("system", "user", model="haiku", pcp_dir=pcp_dir, command="test-call")
    ledger = (pcp_dir / "token_ledger.yaml").read_text()
    assert "test-call" in ledger
