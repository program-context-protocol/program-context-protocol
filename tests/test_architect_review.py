from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from pcp.cli import cli

CLEAN_RESULT = {"findings": [], "summary": "No architecture violations found.", "blocks": 0, "warns": 0}


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "architecture.md").write_text("# Architecture\nKeep modules decoupled.")
    return pcp_dir


def test_no_diff_exits_clean(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.architect_review.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No changes to review" in result.output


def test_clean_review_exits_0(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.architect_review.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+++ b/x.py\n+ok\n", stderr="")
        mock_llm.return_value = CLEAN_RESULT
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No architecture violations found" in result.output


def test_block_finding_with_fail_on_block_exits_1(tmp_path):
    _init_pcp(tmp_path)
    block_result = {
        "findings": [{"severity": "BLOCK", "location": "x.py:10", "principle": "modularity",
                       "finding": "cross-module import", "fix": "use interface"}],
        "summary": "Cross-module coupling found.", "blocks": 1, "warns": 0,
    }
    with patch("pcp.commands.architect_review.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+++ b/x.py\n+bad\n", stderr="")
        mock_llm.return_value = block_result
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path), "--fail-on-block"])
    assert result.exit_code == 1
    assert "modularity" in result.output
    assert "x.py:10" in result.output


def test_block_finding_without_fail_on_block_still_exits_0(tmp_path):
    _init_pcp(tmp_path)
    block_result = {
        "findings": [{"severity": "BLOCK", "location": "x.py:10", "principle": "modularity",
                       "finding": "cross-module import", "fix": "use interface"}],
        "summary": "Cross-module coupling found.", "blocks": 1, "warns": 0,
    }
    with patch("pcp.commands.architect_review.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+++ b/x.py\n+bad\n", stderr="")
        mock_llm.return_value = block_result
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path)])
    assert result.exit_code == 0


def test_module_mode_missing_spec_exits_2(tmp_path):
    _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path), "--module", "nonexistent"])
    assert result.exit_code == 2
    assert "No spec found" in result.output


def test_module_mode_reviews_spec_text(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    mod_dir = pcp_dir / "strategy" / "modules" / "add"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text("module: add\ndescription: adds numbers\n")

    with patch("pcp.llm.client.call_json") as mock_llm:
        mock_llm.return_value = CLEAN_RESULT
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path), "--module", "add"])
        prompt = mock_llm.call_args[0][1]
        assert "adds numbers" in prompt
    assert result.exit_code == 0


def test_json_output_with_fail_on_block(tmp_path):
    _init_pcp(tmp_path)
    with patch("pcp.commands.architect_review.subprocess.run") as mock_run, \
            patch("pcp.llm.client.call_json") as mock_llm:
        mock_run.return_value = MagicMock(returncode=0, stdout="diff --git a/x.py\n+++ b/x.py\n+ok\n", stderr="")
        mock_llm.return_value = CLEAN_RESULT
        runner = CliRunner()
        result = runner.invoke(cli, ["architect-review", "--path", str(tmp_path), "--json"])
    import json
    data = json.loads(result.output)
    assert data["blocks"] == 0
