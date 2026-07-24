"""pcp build-status (2026-07-24): live view of an in-progress pcp build run,
closes the opacity gap that triggered a real ontology-foundry incident
(build killed on 07-21 partly because there was no way to see it working)."""

from datetime import datetime, timedelta, timezone

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.build_status import format_status, load_progress
from pcp.commands.build import _write_progress


def test_load_progress_none_when_missing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert load_progress(pcp_dir) is None


def test_write_progress_then_load_round_trips(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_progress(pcp_dir, "mod", "A1", 2, "coding")
    data = load_progress(pcp_dir)
    assert data == {
        "module": "mod", "criterion_id": "A1", "attempt": 2, "step": "coding",
        "updated_at": data["updated_at"],
    }


def test_format_status_no_progress_file():
    assert "No build in progress" in format_status(None)


def test_format_status_shows_module_criterion_attempt_step():
    now = datetime.now(timezone.utc)
    data = {"module": "mod", "criterion_id": "A1", "attempt": 2, "step": "coding",
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    line = format_status(data, now=now)
    assert "mod/A1 attempt 2 — coding" in line
    assert "updated 0s ago" in line


def test_format_status_flags_stale_step():
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=900)
    data = {"module": "mod", "criterion_id": "A1", "attempt": 1, "step": "qa: evaluating gates",
            "updated_at": old.strftime("%Y-%m-%dT%H:%M:%SZ")}
    line = format_status(data, now=now)
    assert "may be stuck" in line


def test_format_status_done_step_never_flagged_stale():
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=900)
    data = {"module": "mod", "criterion_id": "A1", "attempt": 1, "step": "done",
            "updated_at": old.strftime("%Y-%m-%dT%H:%M:%SZ")}
    line = format_status(data, now=now)
    assert "may be stuck" not in line


def test_cli_build_status_no_progress(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["build-status", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No build in progress" in result.output


def test_cli_build_status_reads_progress_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    _write_progress(pcp_dir, "mod", "A1", 1, "coding")
    runner = CliRunner()
    result = runner.invoke(cli, ["build-status", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "mod/A1 attempt 1 — coding" in result.output


def test_cli_build_status_no_pcp_dir_errors(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["build-status", "--path", str(tmp_path)])
    assert result.exit_code == 2
