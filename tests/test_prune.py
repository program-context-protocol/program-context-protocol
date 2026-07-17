import gzip
import time
from datetime import datetime, timedelta, timezone

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.prune import run_prune, _retention_config
from pcp import telemetry
from pcp.evidence_chain import verify_chain


def _touch_old(path, days_old):
    """Backdate a file's mtime so age-based pruning logic can be tested
    without sleeping in real time."""
    old_time = time.time() - (days_old * 86400)
    import os
    os.utime(path, (old_time, old_time))


def _init_pcp(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


# ── run_prune: pure discovery/deletion logic ──

def test_no_config_finds_nothing(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    result = run_prune(pcp_dir, evidence_days=None, transcript_days=None)
    assert result["total_files"] == 0


def test_finds_evidence_past_retention_window(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    new_file = ev_dir / "lint.txt"
    new_file.write_text("recent output")  # fresh mtime, within window

    result = run_prune(pcp_dir, evidence_days=90, transcript_days=None)
    assert result["evidence_files"] == [old_file]
    assert new_file not in result["evidence_files"]


def test_finds_transcripts_past_retention_window(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    transcripts_dir = pcp_dir / "transcripts"
    transcripts_dir.mkdir()
    old_transcript = transcripts_dir / "session-1.jsonl.gz"
    with gzip.open(old_transcript, "wt") as f:
        f.write("{}\n")
    _touch_old(old_transcript, days_old=200)

    result = run_prune(pcp_dir, evidence_days=None, transcript_days=90)
    assert result["transcript_files"] == [old_transcript]


def test_retention_config_read_from_ci_rules(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0", "rules": [], "retention": {"evidence_days": 30, "transcript_days": 60},
    }))
    config = _retention_config(pcp_dir)
    assert config == {"evidence_days": 30, "transcript_days": 60}


# ── CLI ──

def test_cli_no_config_does_nothing(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No retention configured" in result.output


def test_cli_dry_run_deletes_nothing(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path), "--evidence-days", "90", "--dry-run"])
    assert result.exit_code == 0
    assert old_file.exists()
    assert "nothing deleted" in result.output.lower()


def test_cli_yes_flag_deletes_without_prompt(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path), "--evidence-days", "90", "--yes"])
    assert result.exit_code == 0
    assert not old_file.exists()
    assert (pcp_dir / "prune_log.yaml").exists()
    log = yaml.safe_load((pcp_dir / "prune_log.yaml").read_text())
    assert log["runs"][0]["files_removed"] == 1


def test_cli_declining_confirmation_keeps_files(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path), "--evidence-days", "90"], input="n\n")
    assert result.exit_code == 0
    assert old_file.exists()
    assert "aborted" in result.output.lower()


def test_cli_ci_rules_retention_config_used_without_flags(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    (pcp_dir / "ci_rules.yaml").write_text(yaml.dump({
        "version": "1.0", "rules": [], "retention": {"evidence_days": 30},
    }))
    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path), "--yes"])
    assert result.exit_code == 0
    assert not old_file.exists()


# ── the load-bearing guarantee: pruning evidence/transcripts never touches
#    the hash-chained ledgers, verify_chain() must still pass post-prune ──

def test_prune_never_breaks_telemetry_hash_chain(tmp_path):
    pcp_dir = _init_pcp(tmp_path)
    telemetry.record(pcp_dir, cycle="build", module="widgets", criterion_id="A001", files=[])
    telemetry.record(pcp_dir, cycle="qa", module="widgets", criterion_id="A001", check="test-suite",
                      files=[], result="pass", errors=[], error_count=0)

    ev_dir = pcp_dir / "evidence" / "widgets" / "A001" / "attempt_1"
    ev_dir.mkdir(parents=True)
    old_file = ev_dir / "test-suite.txt"
    old_file.write_text("old output")
    _touch_old(old_file, days_old=100)

    before = verify_chain(telemetry.load(pcp_dir))
    assert before == []

    runner = CliRunner()
    result = runner.invoke(cli, ["prune", "--path", str(tmp_path), "--evidence-days", "90", "--yes"])
    assert result.exit_code == 0
    assert not old_file.exists()

    after = verify_chain(telemetry.load(pcp_dir))
    assert after == []
