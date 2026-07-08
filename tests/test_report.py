import json

import yaml
from click.testing import CliRunner

from pcp.cli import cli


def test_report_no_bypasses_no_current_state(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No bypasses logged" in result.output
    assert "unknown (run pcp scan)" in result.output


def test_report_shows_coverage_and_bypasses(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text("## Drift Score\nacceptance coverage: 0.80\n")
    (pcp_dir / "bypass_log.yaml").write_text(yaml.dump({
        "bypasses": [{"timestamp": "2026-01-01T00:00:00Z", "reason": "known issue", "rules_bypassed": ["R001"]}]
    }))
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "0.80" in result.output
    assert "known issue" in result.output
    assert "R001" in result.output


def test_report_json_output(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "bypass_log.yaml").write_text(yaml.dump({
        "bypasses": [{"timestamp": "t", "reason": "r", "rules_bypassed": ["R001"]}]
    }))
    runner = CliRunner()
    result = runner.invoke(cli, ["report", "--path", str(tmp_path), "--json"])
    data = json.loads(result.output)
    assert data["bypass_count"] == 1
    assert data["bypasses"][0]["reason"] == "r"
