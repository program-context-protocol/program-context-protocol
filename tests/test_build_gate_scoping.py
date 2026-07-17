"""Dogfood-found gate-input fixes (2026-07-17): PCP-operational file exclusion
and criterion-scoped judge framing."""

from unittest.mock import patch

from pcp.commands.build import (
    _criterion_scope_framing,
    _is_pcp_operational,
    _run_gate_check,
)


def test_operational_paths_detected():
    assert _is_pcp_operational(".pcp/token_ledger.yaml")
    assert _is_pcp_operational(".pcp/telemetry.jsonl")
    assert _is_pcp_operational(".pcp/evidence/auth/A1/attempt_1/gate.txt")
    assert _is_pcp_operational(".pcp/transcripts/abc.jsonl.gz")
    assert _is_pcp_operational("./.pcp/token_ledger.yaml")


def test_agent_deliverables_not_operational():
    assert not _is_pcp_operational("src/app.py")
    assert not _is_pcp_operational(".pcp/strategy/modules/auth/acceptance.yaml")
    assert not _is_pcp_operational(".pcp/design_system.md")
    assert not _is_pcp_operational(".pcp/objective.md")


def test_framing_names_criterion_and_partial_build():
    ctx = {"module": "storage", "criterion_id": "A001",
           "criterion_description": "JSON persistence layer", "attempt": 1, "files": []}
    framing = _criterion_scope_framing(ctx)
    assert "A001" in framing
    assert "JSON persistence layer" in framing
    assert "NOT built yet" in framing
    assert "NOT a regression" in framing


def test_gate_check_prompt_carries_framing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("objective")
    ctx = {"module": "storage", "criterion_id": "A001",
           "criterion_description": "JSON persistence layer", "attempt": 1, "files": []}
    captured = {}

    def fake_call_json(system, prompt, **kw):
        captured["prompt"] = prompt
        return {"recommendation": "merge", "alignment_score": 1.0}, {}

    with patch("pcp.llm.client.call_json", side_effect=fake_call_json):
        issues = _run_gate_check(pcp_dir, "diff content", ctx)
    assert issues == []
    assert "ONE acceptance criterion" in captured["prompt"]
    assert "A001" in captured["prompt"]
