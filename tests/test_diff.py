from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.diff import _extract_pending, _extract_coverage_score


def test_extract_pending_finds_unchecked_items(tmp_path):
    cs = tmp_path / "current_state.md"
    cs.write_text("- [x] ADD/A001: done thing\n- [ ] ADD/A002: pending thing\n- [ ] SUB/S001: another pending\n")
    pending = _extract_pending(cs)
    assert pending == ["ADD/A002: pending thing", "SUB/S001: another pending"]


def test_extract_pending_missing_file(tmp_path):
    assert _extract_pending(tmp_path / "nope.md") == []


def test_extract_coverage_score_found(tmp_path):
    cs = tmp_path / "current_state.md"
    cs.write_text("## Drift Score\nacceptance coverage: 0.75\n")
    assert _extract_coverage_score(cs) == "75%"


def test_extract_coverage_score_unknown_when_missing():
    import pathlib
    assert _extract_coverage_score(pathlib.Path("/nonexistent/path.md")) == "unknown"


def test_diff_cli_no_target_state_skips(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No target_state.md" in result.output


def test_diff_cli_no_current_state_exits_2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "target_state.md").write_text("# Target\nFull app.")
    runner = CliRunner()
    result = runner.invoke(cli, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 2


def test_diff_cli_writes_diff_md_with_pending_gaps(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "target_state.md").write_text("# Target\nFull calculator app.")
    (pcp_dir / "current_state.md").write_text(
        "# Current\n- [x] ADD/A001: add works\n- [ ] SUB/S001: subtract works\n\n"
        "## Drift Score\nacceptance coverage: 0.50\n"
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 0

    diff_md = (pcp_dir / "diff.md").read_text()
    assert "SUB/S001: subtract works" in diff_md
    assert "Coverage: 50%" in diff_md
    assert "Implement the pending criteria" in diff_md


def test_diff_cli_no_pending_gaps_says_all_met(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "target_state.md").write_text("# Target\nDone.")
    (pcp_dir / "current_state.md").write_text("# Current\n- [x] ADD/A001: done\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 0
    diff_md = (pcp_dir / "diff.md").read_text()
    assert "No pending criteria" in diff_md
    assert "Advance SDLC phase" in diff_md
