import json

from click.testing import CliRunner

from pcp.cli import cli


def test_report_shows_deprecation_message(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "deprecated" in result.output
    assert "pcp provenance" in result.output
    assert "pcp dashboard" in result.output


def test_report_json_output_signals_deprecation(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert data["deprecated"] is True
    assert "pcp provenance" in data["use_instead"]
    assert "pcp dashboard" in data["use_instead"]


def test_report_errors_without_pcp_dir(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path)])
    assert result.exit_code == 2
