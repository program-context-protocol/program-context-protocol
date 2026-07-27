"""Logic-tier integrity pass (2026-07-18): CTRL-019 mechanism presence,
CTRL-020 rung necessity, tier-distribution policy."""

from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp import telemetry
from pcp.cli import cli
from pcp.commands.build import _run_wave_rung_necessity_check, _run_wave_tier_presence_check
from pcp.commands.validate_strategy import _add_tier_distribution


def _project(tmp_path, criteria, target_content="x = 1\n"):
    pcp_dir = tmp_path / ".pcp"
    mod = pcp_dir / "strategy" / "modules" / "m"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "criteria": criteria}))
    (tmp_path / "impl.py").write_text(target_content)
    return pcp_dir


# ── CTRL-019 tier presence ──

def test_rung2_without_solver_flagged(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "optimize schedule", "status": "complete",
         "logic_tier": 2, "target": "impl.py"},
    ])
    findings = _run_wave_tier_presence_check(pcp_dir, [{"name": "m"}], 0)
    assert len(findings) == 1
    assert "logic_tier=2" in findings[0]


def test_rung2_with_solver_import_clean(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "optimize schedule", "status": "complete",
         "logic_tier": 2, "target": "impl.py"},
    ], target_content="import pulp\nx = 1\n")
    assert _run_wave_tier_presence_check(pcp_dir, [{"name": "m"}], 0) == []


def test_rung5_stdlib_lru_cache_counts(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "cache results", "status": "complete",
         "logic_tier": 5, "target": "impl.py"},
    ], target_content="from functools import lru_cache\n@lru_cache\ndef f():\n    return 1\n")
    assert _run_wave_tier_presence_check(pcp_dir, [{"name": "m"}], 0) == []


def test_rung1_and_rung6_not_presence_checked(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "fixed lookup", "status": "complete", "logic_tier": 1, "target": "impl.py"},
        {"id": "A2", "description": "open-ended judgment", "status": "complete", "logic_tier": 6, "target": "impl.py"},
    ])
    assert _run_wave_tier_presence_check(pcp_dir, [{"name": "m"}], 0) == []


def test_presence_records_telemetry_as_advisory_pass(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "optimize", "status": "complete", "logic_tier": 2, "target": "impl.py"},
    ])
    _run_wave_tier_presence_check(pcp_dir, [{"name": "m"}], 0)
    recs = [r for r in telemetry.load(pcp_dir) if r.get("check") == "wave-tier-presence"]
    assert recs and recs[0]["control_id"] == "CTRL-019"
    assert recs[0]["result"] == "advisory"  # advisory: ran, found something, deliberately did not block.
    # NOT "pass" -- that value is what `pcp provenance` reads, and claiming
    # a clean pass for a check that found things falsifies the audit trail.


# ── CTRL-020 rung necessity ──

def test_rung1_with_judgment_language_flagged_without_llm(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "Recommend the best plan and summarize results",
         "status": "complete", "logic_tier": 1, "target": "impl.py"},
    ])
    with patch("pcp.commands.build.llm.call_json") as mock_call:
        findings = _run_wave_rung_necessity_check(pcp_dir, [{"name": "m"}], 0)
    mock_call.assert_not_called()  # no rung-6 criteria — no LLM spend
    assert len(findings) == 1
    assert "under-declared" in findings[0]


def test_rung6_over_declaration_flagged_by_judge(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "Map ISO country code to currency code",
         "status": "complete", "logic_tier": 6, "target": "impl.py"},
    ])
    judge = {"verdicts": [{"index": 0, "over_declared": True, "cheaper_rung": 1,
                           "reason": "fixed lookup table"}]}
    with patch("pcp.commands.build.llm.call_json", return_value=judge):
        findings = _run_wave_rung_necessity_check(pcp_dir, [{"name": "m"}], 0)
    assert len(findings) == 1
    assert "rung 1 could serve" in findings[0]


def test_rung6_judge_failure_is_silent_advisory_skip(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": "A1", "description": "open judgment", "status": "complete", "logic_tier": 6, "target": "impl.py"},
    ])
    with patch("pcp.commands.build.llm.call_json", side_effect=RuntimeError("down")):
        findings = _run_wave_rung_necessity_check(pcp_dir, [{"name": "m"}], 0)
    assert findings == []  # advisory check degrades, never raises


# ── tier distribution policy ──

def test_tier_distribution_green_when_llm_share_low(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": f"A{i}", "description": "x", "status": "pending", "logic_tier": t, "target": "impl.py"}
        for i, t in enumerate([1, 1, 2, 5, 6])
    ])
    result = _add_tier_distribution(pcp_dir, {})
    assert result["rung6_share"] == 0.2
    assert result["tier_distribution_color"] == "green"


def test_tier_distribution_red_when_llm_heavy(tmp_path):
    pcp_dir = _project(tmp_path, [
        {"id": f"A{i}", "description": "x", "status": "pending", "logic_tier": t, "target": "impl.py"}
        for i, t in enumerate([6, 6, 6, 6, 1])
    ])
    result = _add_tier_distribution(pcp_dir, {})
    assert result["rung6_share"] == 0.8
    assert result["tier_distribution_color"] == "red"


def test_init_scaffolds_tier_distribution_policy(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    rego = tmp_path / ".pcp" / "policies" / "tier_distribution.rego"
    assert rego.exists()
    assert "rung6_share" in rego.read_text()


# ── logic-tier guide (selection/implementation playbook, 2026-07-18) ──

def test_init_scaffolds_logic_tier_guide(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    guide = tmp_path / ".pcp" / "logic_tier_guide.md"
    assert guide.exists()
    text = guide.read_text()
    assert "CORRECTNESS ORACLE" in text
    for rung in range(1, 7):
        assert f"Rung {rung}" in text
    assert "DECOMPOSE FIRST" in text
    assert "search for a way OFF rung 6" in text


def test_build_prompt_points_agent_at_declared_rung_section(tmp_path):
    from pcp.commands.build import _build_agent_prompt
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    prompt = _build_agent_prompt(
        pcp_dir, "m",
        {"id": "A1", "description": "optimize the schedule", "logic_tier": 2},
        {"name": "m"},
    )
    assert "logic_tier=2" in prompt
    assert "Rung 2" in prompt
    assert "logic_tier_guide.md" in prompt
    assert "wave gate checks tier honesty" in prompt


def test_build_prompt_silent_when_no_tier_declared(tmp_path):
    from pcp.commands.build import _build_agent_prompt
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    prompt = _build_agent_prompt(pcp_dir, "m", {"id": "A1", "description": "x"}, {"name": "m"})
    assert "logic_tier_guide" not in prompt
