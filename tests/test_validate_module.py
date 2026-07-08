from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli

VALID_SPEC = {
    "version": "1.0", "module": "add", "description": "Performs addition of two numbers.",
    "objective_coverage": ["Addition"], "dependencies": [], "constraints": [],
}


def _init_pcp(tmp_path, spec=None, deprecated=False):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCalculator app.")
    mod_dir = pcp_dir / "strategy" / "modules" / "add"
    mod_dir.mkdir(parents=True)
    s = dict(spec or VALID_SPEC)
    if deprecated:
        s["deprecated"] = True
    (mod_dir / "spec.yaml").write_text(yaml.dump(s))
    return pcp_dir


def test_module_not_found_exits_2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "strategy" / "modules").mkdir(parents=True)
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-module", "nonexistent", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_deprecated_module_skips(tmp_path):
    _init_pcp(tmp_path, deprecated=True)
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-module", "add", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "deprecated" in result.output


def test_no_objective_exits_2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    mod_dir = pcp_dir / "strategy" / "modules" / "add"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(yaml.dump(VALID_SPEC))
    runner = CliRunner()
    result = runner.invoke(cli, ["validate-module", "add", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "objective.md not found" in result.output


def test_aligned_module_exits_0(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {
            "alignment_score": 0.9, "aligned": True, "gaps": [], "contradictions": [],
            "decomposition_conflicts": [], "suggestions": [],
        }
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-module", "add", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Module spec is aligned" in result.output


def test_misaligned_module_exits_1(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {
            "alignment_score": 0.2, "aligned": False,
            "gaps": ["missing edge case handling"], "contradictions": ["conflicts with X"],
            "decomposition_conflicts": [], "suggestions": [],
        }
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-module", "add", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "missing edge case handling" in result.output
    assert "conflicts with X" in result.output


def test_json_output_exit_code_matches_aligned_flag(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = {"alignment_score": 0.9, "aligned": True}
        runner = CliRunner()
        result = runner.invoke(cli, ["validate-module", "add", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
