import json
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli

MOCK_COVERAGE_FULL = {
    "coverage_gaps": [], "contradictions": [], "overlaps": [], "missing_modules": [],
    "coverage_score": 1.0,
}


def _write_module(pcp_dir, name, description="Does something.", dependencies=None, deprecated=False):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {
        "version": "1.0", "module": name, "description": description,
        "objective_coverage": [description], "dependencies": dependencies or [], "constraints": [],
    }
    if deprecated:
        spec["deprecated"] = True
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild a calculator.")
    (pcp_dir / "strategy").mkdir(exist_ok=True)
    return pcp_dir


def test_no_objective_exits_2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "objective.md not found" in result.output


def test_no_modules_exits_2(tmp_path):
    _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "No module specs found" in result.output


def test_full_coverage_no_coupling_issues_exits_0(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    _write_module(pcp_dir, "subtract")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "All objective areas covered" in result.output


def test_coverage_gap_causes_nonzero_exit(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {
            **MOCK_COVERAGE_FULL,
            "coverage_gaps": [{"area": "subtraction", "quote": "supports subtraction"}],
            "coverage_score": 0.5,
        }
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "Coverage gaps" in result.output


def test_deprecated_module_skipped_from_coupling_and_coverage(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    _write_module(pcp_dir, "legacy_thing", deprecated=True)
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
        # verify the LLM prompt never saw the deprecated module
        prompt_arg = mock_llm.call_args[0][1]
        assert "legacy_thing" not in prompt_arg
    assert result.exit_code == 0


def test_circular_dependency_causes_nonzero_exit_even_with_full_coverage(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "a", dependencies=["b"])
    _write_module(pcp_dir, "b", dependencies=["a"])
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "Coupling violations" in result.output


def test_plain_direct_dependency_is_informational_not_blocking(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "a", dependencies=["b"])
    _write_module(pcp_dir, "b")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "informational, not" in result.output
    assert "blocking" in result.output
    assert "schema errors" not in result.output


def test_json_output_includes_coupling_fields(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path), "--json"])
    output = json.loads(result.output)
    assert "coupling_score" in output
    assert "coverage_score" in output
    assert result.exit_code == 0


def test_coupling_color_uses_opa_policy_when_scaffolded(tmp_path):
    """Proves the coupling color is actually routed through OPA, not just
    coincidentally matching the hardcoded bands -- the policy here returns
    a value the hardcoded logic would never produce."""
    from pcp.commands.validate_strategy import _coupling_color
    with patch("pcp.policy.evaluate") as mock_eval:
        mock_eval.return_value = {"available": True, "undefined": False, "value": "purple"}
        color = _coupling_color(tmp_path / ".pcp", 0.9)
    assert color == "purple"
    mock_eval.assert_called_once_with(
        tmp_path / ".pcp", "data.pcp.coupling.coupling_color", {"coupling_score": 0.9},
    )


def test_coupling_color_falls_back_when_no_policy_scaffolded(tmp_path):
    from pcp.commands.validate_strategy import _coupling_color
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert _coupling_color(pcp_dir, 0.9) == "green"
    assert _coupling_color(pcp_dir, 0.7) == "yellow"
    assert _coupling_color(pcp_dir, 0.3) == "red"


# ── coverage_audit integration (Goodhart mitigation) ──

def test_inconsistent_coverage_score_surfaced_as_warning(tmp_path):
    """A high coverage_score alongside real open gaps is internally
    inconsistent -- must be surfaced, never silently trusted."""
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {
            **MOCK_COVERAGE_FULL,
            "coverage_gaps": [{"area": "subtraction", "quote": "supports subtraction"}],
            "coverage_score": 0.9,
        }
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert "internally" in result.output
    assert "inconsistent" in result.output
    assert (pcp_dir / "coverage_audit.jsonl").exists()


def test_coverage_audit_findings_included_in_json_output(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {
            **MOCK_COVERAGE_FULL,
            "coverage_gaps": [{"area": "x", "quote": "y"}],
            "coverage_score": 0.95,
        }
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path), "--json"])
    output = json.loads(result.output)
    assert len(output["coverage_audit_findings"]) == 1


def test_consistent_coverage_score_no_warning(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert "internally inconsistent" not in result.output
    assert "drifted" not in result.output


# ── deterministic coverage-score assertions (Goodhart mitigation, phase 2) ──

def _init_pcp_with_assertions(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text(
        "# Objective\n\n## What Success Looks Like\n"
        "1. Customers can complete checkout with a saved payment method\n"
        "2. Administrators can generate quarterly export reports\n"
    )
    (pcp_dir / "strategy").mkdir(exist_ok=True)
    return pcp_dir


def test_objective_with_numbered_assertions_uses_deterministic_scoring(tmp_path):
    pcp_dir = _init_pcp_with_assertions(tmp_path)
    _write_module(pcp_dir, "checkout", description="Handles checkout completion using a saved payment method")
    _write_module(pcp_dir, "reporting", description="Generates quarterly export reports for administrators")
    with patch("pcp.llm.client.call_json") as mock_llm:
        # LLM's own coverage_score is deliberately wrong (0.1) -- deterministic
        # scoring must win, proving this isn't just passing through the LLM value.
        mock_llm.return_value = {**MOCK_COVERAGE_FULL, "coverage_score": 0.1}
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path), "--json"])
    output = json.loads(result.output)
    assert output["scoring_method"] == "deterministic"
    assert output["coverage_score"] == 1.0
    assert output["llm_coverage_score"] == 0.1
    assert output["assertions_total"] == 2
    assert output["assertions_covered"] == 2


def test_objective_without_assertions_falls_back_to_llm_scoring(tmp_path):
    pcp_dir = _init_pcp(tmp_path)  # old-format objective.md, no numbered list
    _write_module(pcp_dir, "add")
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {**MOCK_COVERAGE_FULL, "coverage_score": 0.85}
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path), "--json"])
    output = json.loads(result.output)
    assert output["scoring_method"] == "llm"
    assert output["coverage_score"] == 0.85
    assert "llm_coverage_score" not in output


def test_deterministic_gap_shown_in_cli_output(tmp_path):
    pcp_dir = _init_pcp_with_assertions(tmp_path)
    _write_module(pcp_dir, "checkout", description="Handles checkout completion using a saved payment method")
    # No module covers the reporting assertion -- deterministic scoring must
    # catch this even if the LLM (mocked here to claim full coverage) doesn't.
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = dict(MOCK_COVERAGE_FULL)
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-strategy", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "deterministic" in result.output
    assert "quarterly export reports" in result.output
