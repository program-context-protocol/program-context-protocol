"""Architect pre-flight (CTRL-032, swarm-role backlog, 2026-07-20) -- a
pre-implementation sanity check for HIGH-RISK criteria only (logic_tier >=
5, or build_vs_buy of reuse_whole/fork_adapt), before any code is written."""

from unittest.mock import patch

from pcp import telemetry
from pcp.commands.build import _run_architect_preflight


def _mod(name="widgets"):
    return {"name": name, "spec": {"description": "handles widgets", "dependencies": [], "constraints": []}}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def test_preflight_noop_for_low_risk_criterion(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A1", "description": "x", "logic_tier": 1, "build_vs_buy": {"decision": "build_fresh"}}
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        result = _run_architect_preflight(pcp_dir, _mod(), criterion)
    assert result == []
    mock_call.assert_not_called()
    assert not [r for r in _qa_records(pcp_dir) if r["check"] == "architect-preflight"]


def test_preflight_runs_for_high_logic_tier(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A1", "description": "x", "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh"}}
    with patch("pcp.commands.build.llm.call_json", return_value={"findings": []}) as mock_call:
        result = _run_architect_preflight(pcp_dir, _mod(), criterion)
    assert result == []
    mock_call.assert_called_once()
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "architect-preflight"][0]
    assert record["control_id"] == "CTRL-032"
    assert record["result"] == "pass"


def test_preflight_runs_for_reuse_whole_build_vs_buy(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A1", "description": "x", "logic_tier": 1, "build_vs_buy": {"decision": "reuse_whole"}}
    with patch("pcp.commands.build.llm.call_json", return_value={"findings": []}) as mock_call:
        _run_architect_preflight(pcp_dir, _mod(), criterion)
    mock_call.assert_called_once()


def test_preflight_returns_rendered_findings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A1", "description": "x", "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh"}}
    fake_response = {"findings": [{"concern": "rung mismatch", "suggestion": "reconsider tier"}]}
    with patch("pcp.commands.build.llm.call_json", return_value=fake_response):
        result = _run_architect_preflight(pcp_dir, _mod(), criterion)
    assert result == ["rung mismatch — reconsider tier"]
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "architect-preflight"][0]
    assert record["result"] == "advisory"  # advisory: ran, found something, deliberately did not block.
    # NOT "pass" -- that value is what `pcp provenance` reads, and claiming
    # a clean pass for a check that found things falsifies the audit trail.
    assert record["error_count"] == 1


def test_preflight_never_raises_on_llm_failure(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    criterion = {"id": "A1", "description": "x", "logic_tier": 6, "build_vs_buy": {"decision": "build_fresh"}}
    with patch("pcp.commands.build.llm.call_json", side_effect=RuntimeError("timeout")):
        result = _run_architect_preflight(pcp_dir, _mod(), criterion)
    assert result == []
