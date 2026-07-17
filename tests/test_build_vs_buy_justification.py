import yaml

from pcp.commands.build import _run_build_vs_buy_justification_check
from pcp import telemetry


def _mod(pcp_dir, name="widgets", build_vs_buy=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    acc_path = mod_dir / "acceptance.yaml"
    criterion = {"id": "A001", "description": "Does a thing"}
    if build_vs_buy is not None:
        criterion["build_vs_buy"] = build_vs_buy
    acc_path.write_text(yaml.dump({"criteria": [criterion]}))
    return {"name": name, "acc_path": acc_path}


def _ctx(module="widgets", criterion_id="A001", attempt=1):
    return {"module": module, "submodule": None, "criterion_id": criterion_id, "attempt": attempt, "files": []}


def _qa_records(pcp_dir):
    return [r for r in telemetry.load(pcp_dir) if r.get("cycle") == "qa"]


def test_flags_empty_rationale(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "build_fresh", "rationale": ""})
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert len(findings) == 1
    assert "(empty)" in findings[0]
    record = [r for r in _qa_records(pcp_dir) if r["check"] == "build-vs-buy-justification"][0]
    assert record["control_id"] == "CTRL-017"
    assert record["result"] == "block"


def test_flags_known_placeholder_phrase(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "build_fresh", "rationale": "TODO"})
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert len(findings) == 1


def test_flags_the_literal_unfilled_prompt_template_text(tmp_path):
    """The exact string kickoff.py's SYSTEM_PROMPT ships as the template
    placeholder -- a real failure mode if a generation call echoes the
    template back verbatim instead of filling it in."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "build_fresh", "rationale": "Why this decision, one sentence."})
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert len(findings) == 1


def test_flags_too_short_rationale(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "reuse_whole", "rationale": "seemed fine"})
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert len(findings) == 1


def test_real_rationale_not_flagged(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={
        "decision": "reuse_whole",
        "rationale": "networkx already solves graph cycle detection correctly, no need to reimplement it.",
    })
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []


def test_not_applicable_decision_never_flagged_even_with_no_rationale(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "not_applicable", "rationale": ""})
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []


def test_missing_build_vs_buy_field_not_flagged_by_this_check(tmp_path):
    """Absence entirely is a schema-validation failure elsewhere, not this
    check's job -- this only judges the substance of what IS there."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy=None)
    criterion = {"id": "A001", "description": "x"}
    findings = _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    assert findings == []


def test_no_llm_call_ever_made(tmp_path):
    """Deterministic by design -- build_vs_buy applies to every criterion,
    so an LLM call here would violate Token Discipline the way
    design_justification's UI-only scoping avoids."""
    from unittest.mock import patch
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod = _mod(pcp_dir, build_vs_buy={"decision": "build_fresh", "rationale": "x"})
    criterion = {"id": "A001", "description": "x"}
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        _run_build_vs_buy_justification_check(pcp_dir, mod, criterion, _ctx())
    mock_call.assert_not_called()
