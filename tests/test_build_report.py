"""Build Cycle Report (2026-07-24): surfaces the evidence pcp build already
generates (run_log proof-of-delivery) at the end of a run instead of one
dim summary line nobody opens."""

import subprocess
from pathlib import Path

from pcp import build_report, run_log


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("Objective v1")
    (pcp_dir / "target_state.md").write_text("Target v1")
    (tmp_path / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    return pcp_dir


def test_render_empty_when_nothing_completed_since(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    text = build_report.render(pcp_dir, since_ts="2099-01-01T00:00:00Z")
    assert "No criteria completed this run" in text


def test_render_includes_succeeded_criterion_with_evidence_pointer(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(pcp_dir, module="mod", feature="A1: does the thing", run_type="dev", actor="pcp-build-agent")
    (tmp_path / "impl.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: A1"], cwd=tmp_path, capture_output=True)
    run_log.end_run(pcp_dir, run_id, result="success", tests_ran=True, tests_passed=True,
                     real_gates_passed=["tests", "lint"], llm_judged_gates_passed=["arch"])

    text = build_report.render(pcp_dir, since_ts="2020-01-01T00:00:00Z")
    assert "1/1 criteria succeeded" in text
    assert "mod: A1: does the thing" in text
    assert "tests, lint" in text
    assert ".pcp/evidence/mod/" in text
    assert "Committed: True" in text


def test_render_surfaces_anomalies(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(pcp_dir, module="mod", feature="A1: x", run_type="dev", actor="pcp-build-agent")
    entry = run_log.end_run(pcp_dir, run_id, result="success")  # no commit -> anomalies
    assert entry["anomaly_flags"]

    text = build_report.render(pcp_dir, since_ts="2020-01-01T00:00:00Z")
    assert "Anomalies" in text
    assert "no_commit" in text


def test_render_since_ts_excludes_prior_runs(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(pcp_dir, module="mod", feature="A1: old", run_type="dev", actor="pcp-build-agent")
    run_log.end_run(pcp_dir, run_id, result="success")

    text = build_report.render(pcp_dir, since_ts="2099-01-01T00:00:00Z")
    assert "No criteria completed this run" in text


def test_write_creates_build_report_md(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    out = build_report.write(pcp_dir, since_ts="2020-01-01T00:00:00Z")
    assert out == pcp_dir / "build_report.md"
    assert out.exists()
    assert "Build Cycle Report" in out.read_text()
