"""CTRL-042 wiring in build.py -- _run_native_bridge_cross_model_check.
Additive to CTRL-041 (adversarial review), auto-triggered, no opt-in field.
See native_bridge_verify.py's module docstring for the incident this closes."""

from unittest.mock import patch

from pcp.commands.build import _run_native_bridge_cross_model_check
from pcp import telemetry


def _mod():
    return {"name": "websocket"}


def _crit(logic_tier=1, target="dlls/websocket/websocket.c"):
    return {"id": "A001", "description": "WebSocketReceive delivers real inbound frames.",
            "logic_tier": logic_tier, "target": target}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("check") == "native-bridge-cross-model-review"]


def test_noop_when_criterion_has_no_target(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    c = _crit()
    del c["target"]
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), c, "diff content")
    assert findings == []
    mock_review.assert_not_called()


def test_noop_when_target_file_missing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(), "diff content")
    assert findings == []
    mock_review.assert_not_called()


def test_noop_when_target_has_no_native_bridge_pattern(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    target = tmp_path / "dlls/websocket/websocket.c"
    target.parent.mkdir(parents=True)
    target.write_text("int add(int a, int b) { return a + b; }\n")
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(), "diff content")
    assert findings == []
    mock_review.assert_not_called()


def test_noop_when_logic_tier_is_not_1(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    target = tmp_path / "dlls/websocket/websocket.c"
    target.parent.mkdir(parents=True)
    target.write_text("WINE_UNIX_CALL(unix_send, &params);\n")
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(logic_tier=6), "diff content")
    assert findings == []
    mock_review.assert_not_called()


def test_noop_when_diff_is_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    target = tmp_path / "dlls/websocket/websocket.c"
    target.parent.mkdir(parents=True)
    target.write_text("WINE_UNIX_CALL(unix_send, &params);\n")
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(), "   ")
    assert findings == []
    mock_review.assert_not_called()


def test_fires_and_returns_findings_when_triggered_and_disputed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    target = tmp_path / "dlls/websocket/websocket.c"
    target.parent.mkdir(parents=True)
    target.write_text("WINE_UNIX_CALL(unix_send, &params);\n")
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        mock_review.return_value = ["single read() call may not get the full multi-part response"]
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(), "diff content")
    assert len(findings) == 1
    assert "CTRL-042" in findings[0]
    assert "single read() call may not get the full multi-part response" in findings[0]
    mock_review.assert_called_once()
    records = _qa_records(pcp_dir)
    assert len(records) == 1
    assert records[0]["result"] == "block"
    assert records[0]["control_id"] == "CTRL-042"


def test_fires_and_records_pass_when_triggered_and_clean(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    target = tmp_path / "dlls/websocket/websocket.c"
    target.parent.mkdir(parents=True)
    target.write_text("WINE_UNIX_CALL(unix_send, &params);\n")
    with patch("pcp.native_bridge_verify.run_cross_model_review") as mock_review:
        mock_review.return_value = []
        findings = _run_native_bridge_cross_model_check(pcp_dir, tmp_path, _mod(), _crit(), "diff content")
    assert findings == []
    records = _qa_records(pcp_dir)
    assert len(records) == 1
    assert records[0]["result"] == "pass"
