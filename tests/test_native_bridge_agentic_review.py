"""CTRL-043 (2026-08-09) -- cross-vendor, execution-capable upgrade to
CTRL-042's text-only review. Real agy agentic session (Bash/Read tool
access), worktree-isolated (the actual safety mechanism, since
--dangerously-skip-permissions is required for real shell execution and
auto-approves every tool call). Auto-triggered, same condition as CTRL-042.
See build.py's _run_native_bridge_agentic_review docstring for the full
design/risk reasoning."""

import json
from unittest.mock import patch

import pytest

from pcp.commands.build import _run_native_bridge_agentic_review, _BuildBudget, BudgetExceeded
from pcp import telemetry


@pytest.fixture(autouse=True)
def _fake_review_worktree(tmp_path):
    with patch("pcp.commands.build._setup_review_worktree", return_value=tmp_path), \
         patch("pcp.commands.build._cleanup_review_worktree"):
        yield


def _mod():
    return {"name": "websocket"}


def _crit(logic_tier=1, target="dlls/websocket/websocket.c"):
    return {"id": "A001", "description": "WebSocketReceive delivers real inbound frames.",
            "logic_tier": logic_tier, "target": target}


def _agy_envelope(result_obj, conversation_id="agy-1", tokens=5000):
    return json.dumps({
        "conversation_id": conversation_id, "status": "SUCCESS",
        "response": json.dumps(result_obj),
        "usage": {"input_tokens": tokens // 2, "output_tokens": tokens // 2, "total_tokens": tokens},
    })


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("check") == "native-bridge-agentic-review"]


def _write_target(tmp_path, path, content):
    p = tmp_path / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_noop_when_no_native_bridge_pattern(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "int add(int a, int b) { return a + b; }\n")
    budget = _BuildBudget(10)
    with patch("pcp.commands.build.subprocess.Popen") as mock_popen:
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff", budget)
    assert findings == []
    mock_popen.assert_not_called()


def test_noop_when_logic_tier_not_1(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)
    with patch("pcp.commands.build.subprocess.Popen") as mock_popen:
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(logic_tier=6), "diff", budget)
    assert findings == []
    mock_popen.assert_not_called()


def test_budget_circuit_breaker_skips_gracefully(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(0)
    with patch("pcp.commands.build.subprocess.Popen") as mock_popen:
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff", budget)
    assert findings == []
    mock_popen.assert_not_called()


def test_worktree_setup_failure_records_error_and_skips(tmp_path):
    """Worktree setup failing legitimately triggers a real telemetry write
    (_qa_record), which itself may shell out to `chflags` (evidence_chain's
    append-only enforcement) -- so the real assertion here is "agy was
    never spawned", not "Popen was never called at all"."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)
    with patch("pcp.commands.build._setup_review_worktree", return_value=None), \
         patch("pcp.commands.build.subprocess.Popen") as mock_popen:
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff", budget)
    assert findings == []
    agy_calls = [c for c in mock_popen.call_args_list if "agy" in str(c.args[0][0])]
    assert agy_calls == []
    records = _qa_records(pcp_dir)
    assert len(records) == 1
    assert records[0]["result"] == "error"


def test_clean_review_produces_no_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self._cmd = cmd
            self.returncode = 0
            self.pid = 1
        def communicate(self, input=None, timeout=None):
            return _agy_envelope({"is_real": True, "confidence": 0.9, "red_flags": [], "reasoning": "genuinely works"}), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert findings == []
    records = _qa_records(pcp_dir)
    assert len(records) == 1
    assert records[0]["result"] == "pass"


def test_disputed_review_returns_findings_and_records_block(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            return _agy_envelope({
                "is_real": False, "confidence": 0.85,
                "red_flags": ["single read() call assumes full message in one shot"],
                "reasoning": "traced the daemon's 3 separate writes against the client's 2 reads",
            }), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)

    assert len(findings) == 1
    assert "CTRL-043" in findings[0]
    assert "single read() call assumes full message in one shot" in findings[0]
    records = _qa_records(pcp_dir)
    assert records[0]["result"] == "block"
    assert records[0]["control_id"] == "CTRL-043"


def test_fenced_response_still_parses(tmp_path):
    """Confirmed live (2026-08-09 smoke test): agy sometimes wraps its JSON
    response in markdown fences even when told not to -- same behavior
    llm.call_json already handles for other harnesses."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            fenced = "```json\n" + json.dumps({"is_real": True, "confidence": 0.7, "red_flags": [], "reasoning": "ok"}) + "\n```"
            return json.dumps({
                "conversation_id": "agy-2", "status": "SUCCESS", "response": fenced,
                "usage": {"total_tokens": 1000},
            }), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)
    assert findings == []


def test_timeout_fails_open(tmp_path):
    import subprocess as subprocess_mod
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 1
        def communicate(self, input=None, timeout=None):
            if timeout is not None:
                raise subprocess_mod.TimeoutExpired(cmd="agy", timeout=timeout)
            return "", ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen), \
         patch("pcp.commands.build.os.killpg"), patch("pcp.commands.build.os.getpgid", return_value=1):
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)
    assert findings == []
    records = _qa_records(pcp_dir)
    assert records[0]["result"] == "error"


def test_token_ceiling_exceeded_treated_as_error(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            return _agy_envelope({"is_real": True, "confidence": 0.9, "red_flags": [], "reasoning": "ok"}, tokens=999999999), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        findings = _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)
    assert findings == []
    records = _qa_records(pcp_dir)
    assert records[0]["result"] == "error"


def test_cleanup_review_worktree_always_called(tmp_path):
    """finally block must run regardless of outcome -- disputed, clean, or
    error, the disposable worktree must never leak."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            self.returncode = 1
        def communicate(self, input=None, timeout=None):
            return "", "crashed"

    with patch("pcp.commands.build._setup_review_worktree", return_value=tmp_path) as mock_setup, \
         patch("pcp.commands.build._cleanup_review_worktree") as mock_cleanup, \
         patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)
    mock_setup.assert_called_once()
    mock_cleanup.assert_called_once()


def test_uses_dangerously_skip_permissions_not_accept_edits(tmp_path):
    """The real distinction found live: accept-edits alone blocks shell
    execution in headless mode -- this check must use the flag that
    actually enables it, and only because worktree isolation makes that
    safe."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_target(tmp_path, "dlls/websocket/websocket.c", "WINE_UNIX_CALL(unix_send, &params);\n")
    budget = _BuildBudget(10)
    captured_cmd = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured_cmd.extend(cmd)
            self.returncode = 0
        def communicate(self, input=None, timeout=None):
            return _agy_envelope({"is_real": True, "confidence": 0.9, "red_flags": [], "reasoning": "ok"}), ""

    with patch("pcp.commands.build.subprocess.Popen", side_effect=_FakePopen):
        _run_native_bridge_agentic_review(pcp_dir, tmp_path, _mod(), _crit(), "diff content", budget)
    assert "--dangerously-skip-permissions" in captured_cmd
