import json
import subprocess
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from pcp.cli import cli

MOCK_RESULT = {
    "alignment_score": 0.85,
    "summary": "Adds the subtract module cleanly.",
    "advances": ["Implements subtract per spec"],
    "regressions": [],
    "llm_rule_violations": [],
    "recommendation": "merge",
}


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nCalculator app.")
    (pcp_dir / "target_state.md").write_text("# Target\nFull calculator.")
    (pcp_dir / "current_state.md").write_text("# Current\n50% complete.")
    return pcp_dir


def test_gate_no_diff_exits_clean(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.gate.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = CliRunner()
        result = runner.invoke(cli, ["gate", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No diff" in result.output


def test_gate_git_diff_failure_exits_2(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.gate.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="fatal: bad revision")
        runner = CliRunner()
        result = runner.invoke(cli, ["gate", "--path", str(tmp_path)])
    assert result.exit_code == 2


def test_gate_json_output(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.gate.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+added\n", stderr="")
        mock_llm.return_value = MOCK_RESULT
        runner = CliRunner()
        result = runner.invoke(cli, ["gate", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    output = json.loads(result.output)
    assert output["alignment_score"] == 0.85
    assert output["recommendation"] == "merge"


def test_gate_markdown_output_never_blocks_language(tmp_path):
    _init_pcp(tmp_path)
    block_result = {**MOCK_RESULT, "alignment_score": 0.1, "recommendation": "block",
                    "regressions": ["Contradicts objective"]}
    with patch("pcp.commands.gate.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+bad\n", stderr="")
        mock_llm.return_value = block_result
        runner = CliRunner()
        result = runner.invoke(cli, ["gate", "--path", str(tmp_path), "--markdown"])
    assert result.exit_code == 0  # advisory -- never a nonzero exit even on "block" recommendation
    assert "advisory only" in result.output.lower()
    assert "🚫" in result.output


def test_gate_terminal_output_shows_regressions(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.gate.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+x\n", stderr="")
        mock_llm.return_value = {**MOCK_RESULT, "regressions": ["moves away from spec"]}
        runner = CliRunner()
        result = runner.invoke(cli, ["gate", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "moves away from spec" in result.output
    assert "does not block merge" in result.output
