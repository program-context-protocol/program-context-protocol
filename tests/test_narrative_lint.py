"""CTRL-036 narrative lint: deterministic stale-date/missing-file checks,
semantic contradiction check (mocked judge), CLI command, wave-merge wiring."""

from unittest.mock import patch

from click.testing import CliRunner

from pcp import narrative_lint, telemetry
from pcp.cli import cli
from pcp.commands.build import _run_wave_narrative_lint_check


# ── find_claude_md_files ──

def test_finds_root_and_nested_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("root\n")
    sub = tmp_path / "apps" / "api"
    sub.mkdir(parents=True)
    (sub / "CLAUDE.md").write_text("nested\n")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "notes.md").write_text("notes\n")
    found = narrative_lint.find_claude_md_files(tmp_path)
    assert len(found) == 3


def test_skips_git_and_node_modules(tmp_path):
    for d in (".git", "node_modules"):
        p = tmp_path / d
        p.mkdir()
        (p / "CLAUDE.md").write_text("x\n")
    (tmp_path / "CLAUDE.md").write_text("root\n")
    found = narrative_lint.find_claude_md_files(tmp_path)
    assert len(found) == 1


# ── check_stale_dates ──

def test_flags_stale_date(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("## Known Issue (2020-01-01, RESOLVED)\nsome text\n")
    findings = narrative_lint.check_stale_dates([f])
    assert len(findings) == 1
    assert "2020-01-01" in findings[0]


def test_recent_date_not_flagged(tmp_path):
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    f = tmp_path / "CLAUDE.md"
    f.write_text(f"## Note ({recent})\n")
    assert narrative_lint.check_stale_dates([f]) == []


# ── check_missing_files ──

def test_flags_missing_referenced_path(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("See `docs/gone.md` for details.\n")
    findings = narrative_lint.check_missing_files([f], tmp_path)
    assert len(findings) == 1
    assert "docs/gone.md" in findings[0]


def test_existing_referenced_path_not_flagged(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "real.md").write_text("x\n")
    f = tmp_path / "CLAUDE.md"
    f.write_text("See `docs/real.md` for details.\n")
    assert narrative_lint.check_missing_files([f], tmp_path) == []


def test_url_and_slash_command_not_flagged(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("Visit `https://example.com/foo/bar` or run `/pcp`.\n")
    assert narrative_lint.check_missing_files([f], tmp_path) == []


# ── collect_status_lines ──

def test_collects_status_shaped_lines(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("- Pending: ship the widget\n- Done: shipped the gadget\n")
    hits = narrative_lint.collect_status_lines([f])
    assert len(hits) == 1
    assert "Pending" in hits[0][2]


# ── check_narrative_contradictions ──

def test_no_status_lines_skips_llm_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.call_json") as mock_call:
        findings = narrative_lint.check_narrative_contradictions(pcp_dir, [])
    mock_call.assert_not_called()
    assert findings == []


def test_no_tracked_state_skips_llm_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.call_json") as mock_call:
        findings = narrative_lint.check_narrative_contradictions(pcp_dir, [("f", 1, "Pending: X")])
    mock_call.assert_not_called()
    assert findings == []


def test_judge_flags_contradiction(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text("Module X: 100% complete, shipped.\n")
    status_lines = [("CLAUDE.md", 3, "Pending: build module X")]
    judge = {"contradictions": [{"index": 0, "reason": "current_state.md shows X already shipped"}]}
    with patch("pcp.llm.client.call_json", return_value=judge):
        findings = narrative_lint.check_narrative_contradictions(pcp_dir, status_lines)
    assert len(findings) == 1
    assert "already shipped" in findings[0]


def test_judge_failure_is_silent_advisory_skip(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "current_state.md").write_text("x\n")
    status_lines = [("CLAUDE.md", 1, "Pending: X")]
    with patch("pcp.llm.client.call_json", side_effect=RuntimeError("down")):
        findings = narrative_lint.check_narrative_contradictions(pcp_dir, status_lines)
    assert findings == []


# ── run() / skip_llm ──

def test_run_skip_llm_omits_contradictions(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "CLAUDE.md").write_text("- Pending: X\n")
    with patch("pcp.llm.client.call_json") as mock_call:
        result = narrative_lint.run(pcp_dir, skip_llm=True)
    mock_call.assert_not_called()
    assert result["contradictions"] == []


# ── CLI command ──

def test_narrative_lint_cli_writes_report_and_telemetry(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (tmp_path / "CLAUDE.md").write_text("- Pending: X\n")
    runner = CliRunner()
    result = runner.invoke(cli, ["narrative-lint", "--path", str(tmp_path), "--skip-llm"])
    assert result.exit_code == 0
    report = (pcp_dir / "narrative_lint.md").read_text()
    assert "Narrative Lint" in report
    recs = [r for r in telemetry.load(pcp_dir) if r.get("check") == "narrative-lint"]
    assert recs and recs[0]["control_id"] == "CTRL-036"


def test_narrative_lint_cli_no_pcp_dir_errors(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["narrative-lint", "--path", str(tmp_path)])
    assert result.exit_code == 2


# ── wave-merge wiring ──

def test_wave_check_records_telemetry_as_advisory_pass(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    stale_ref = tmp_path / "CLAUDE.md"
    stale_ref.write_text("## Note (2020-01-01)\n")
    with patch("pcp.llm.client.call_json") as mock_call:
        mock_call.return_value = {"contradictions": []}
        findings = _run_wave_narrative_lint_check(pcp_dir, 0)
    assert len(findings) == 1  # stale date
    recs = [r for r in telemetry.load(pcp_dir) if r.get("check") == "wave-narrative-lint"]
    assert recs and recs[0]["control_id"] == "CTRL-036"
    assert recs[0]["result"] == "advisory"  # advisory: ran, found something, deliberately did not block.
    # NOT "pass" -- that value is what `pcp provenance` reads, and claiming
    # a clean pass for a check that found things falsifies the audit trail.
