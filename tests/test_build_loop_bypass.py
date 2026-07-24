"""CTRL-037 build-loop bypass detector (2026-07-24): flags when git commits
keep landing well past telemetry.jsonl's last entry -- the signature of
pcp build's formal gated loop silently going unused, real ontology-foundry
incident recurring 3x."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pcp import build_loop_bypass, telemetry


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def _commit(tmp_path, msg, when=None):
    f = tmp_path / f"{msg.replace(' ', '_')}.txt"
    f.write_text(msg)
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    env = None
    if when:
        import os
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "commit", "-m", msg], cwd=tmp_path, capture_output=True, env=env)


def test_inert_with_no_telemetry_history(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    _commit(tmp_path, "init")
    assert build_loop_bypass.check(pcp_dir, tmp_path) == []


def test_inert_within_grace_window(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    telemetry.record(pcp_dir, cycle="qa", check="tests", control_id="CTRL-001", result="pass")
    _commit(tmp_path, "recent work")
    assert build_loop_bypass.check(pcp_dir, tmp_path, threshold_days=3) == []


def test_flags_commits_past_threshold_with_no_telemetry(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    telemetry.record(pcp_dir, cycle="qa", check="tests", control_id="CTRL-001", result="pass", timestamp=old_ts)

    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _commit(tmp_path, "bypassed work", when=recent)

    findings = build_loop_bypass.check(pcp_dir, tmp_path, threshold_days=3)
    assert len(findings) == 1
    assert "commit(s) landed since" in findings[0]


def test_no_git_repo_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    telemetry.record(pcp_dir, cycle="qa", check="tests", control_id="CTRL-001", result="pass")
    assert build_loop_bypass.check(pcp_dir, tmp_path) == []
