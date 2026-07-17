"""Phase 3 loop-intelligence items (2026-07-17 build plan)."""

from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp import telemetry
from pcp.cli import cli
from pcp.commands.build import _complexity_route, _verify_block_findings
from pcp.symbols import diff_fingerprints, fingerprint_python_file


# ── 3.1 complexity routing (report-first) ──

def _mod(deps=0):
    return {"name": "m", "spec": {"dependencies": [f"d{i}" for i in range(deps)]}}


def test_simple_criterion_not_routed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    route, signal = _complexity_route(pcp_dir, _mod(), {"id": "A1", "description": "add a field"})
    assert route is False


def test_complex_criterion_routed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    desc = ("Implement distributed real-time websocket integration with transaction "
            "handling and cache invalidation across services, coordinating concurrent "
            "writers and migration of the existing store to the new encrypted format "
            "with rollback support and backpressure management for the streaming layer.")
    route, signal = _complexity_route(pcp_dir, _mod(deps=3), {"id": "A1", "description": desc})
    assert route is True
    assert signal["score"] >= 3


def test_routing_history_signal(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    for attempt in (1, 2, 3, 1, 2, 3):
        telemetry.record(pcp_dir, cycle="build", cycle_number=attempt, module="m", criterion_id="X")
    _, signal = _complexity_route(pcp_dir, _mod(), {"id": "A1", "description": "short"})
    assert signal["module_retry_ratio"] > 0.4


# ── 3.3 verifier ensemble ──

def test_ensemble_disagreement_keeps_finding(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_VERIFIER_ENSEMBLE", "1")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    responses = [
        ({"verdicts": [{"index": 0, "refuted": True, "reason": "cannot confirm"}]}, {}),   # adversarial: refute
        ({"verdicts": [{"index": 0, "refuted": False}]}, {}),                              # confirmer: holds up
    ]
    with patch("pcp.commands.build.llm.call_json", side_effect=responses):
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff", ["conceptual finding"], {"attempt": 1, "module": "m", "criterion_id": "A1", "files": []},
            "gate", "CTRL-006",
        )
    assert len(kept) == 1
    assert "verifier disagreement" in kept[0]
    assert dropped == []


def test_ensemble_agreement_still_drops(tmp_path, monkeypatch):
    monkeypatch.setenv("PCP_VERIFIER_ENSEMBLE", "1")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    responses = [
        ({"verdicts": [{"index": 0, "refuted": True, "reason": "ungrounded"}]}, {}),
        ({"verdicts": [{"index": 0, "refuted": True, "reason": "confirmed ungrounded"}]}, {}),
    ]
    with patch("pcp.commands.build.llm.call_json", side_effect=responses):
        kept, dropped = _verify_block_findings(
            pcp_dir, "diff", ["conceptual finding"], {"attempt": 1, "module": "m", "criterion_id": "A1", "files": []},
            "gate", "CTRL-006",
        )
    assert kept == []
    assert len(dropped) == 1


# ── 3.4 symbol fingerprints ──

def test_symbol_fingerprint_detects_body_change_ignores_whitespace(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("def a():\n    return 1\n\ndef b():\n    return 2\n")
    fp1 = fingerprint_python_file(f)
    f.write_text("def a():\n\n    return 1\n\n\ndef b():\n    return 3\n")  # a: whitespace only; b: real change
    fp2 = fingerprint_python_file(f)
    assert fp1["a"] == fp2["a"]
    assert fp1["b"] != fp2["b"]


def test_diff_fingerprints_categories():
    old = {"x.py": {"a": "1", "b": "2"}}
    new = {"x.py": {"a": "9", "c": "3"}}
    d = diff_fingerprints(old, new)
    assert d["changed"] == ["x.py:a"]
    assert d["added"] == ["x.py:c"]
    assert d["removed"] == ["x.py:b"]


# ── 3.7 tool-call guard scaffold ──

def test_init_scaffolds_pretooluse_guard(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    guard = tmp_path / ".pcp" / "hooks" / "pretooluse_guard.sh"
    assert guard.exists()
    text = guard.read_text()
    assert '"permissionDecision":"deny"' in text
    assert "auto-ALLOWS anything is an approval bypass" in text  # deny-only design documented
