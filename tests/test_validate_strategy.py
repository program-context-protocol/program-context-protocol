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
