import json

from click.testing import CliRunner

from pcp.cli import cli
from pcp import telemetry


def test_telemetry_cmd_no_records(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["telemetry", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "pcp build" in result.output


def test_telemetry_cmd_table_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="build", module="add", criterion_id="A001",
                      token_input=100, token_output=50, cost_usd=0.01, languages=["Python"])
    telemetry.record(pcp_dir, cycle="qa", module="add", result="pass")
    telemetry.record(pcp_dir, cycle="qa", module="add", result="block")

    runner = CliRunner()
    result = runner.invoke(cli, ["telemetry", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "add" in result.output
    assert "1/2" in result.output  # qa_blocks/qa_total
    assert "3 total records" in result.output


def test_telemetry_cmd_json_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="build", module="add", criterion_id="A001", token_input=100)

    runner = CliRunner()
    result = runner.invoke(cli, ["telemetry", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert data["add"]["attempts"] == 1
    assert data["add"]["criteria"] == ["A001"]
