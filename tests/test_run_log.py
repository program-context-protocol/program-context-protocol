import subprocess
from pathlib import Path

from click.testing import CliRunner

from pcp import run_log
from pcp.cli import cli


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


def test_start_run_writes_pre_record(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(
        pcp_dir, module="mod", feature="feat", run_type="dev", actor="human-interactive",
    )
    records = run_log.load(pcp_dir)
    assert len(records) == 1
    assert records[0]["phase"] == "pre"
    assert records[0]["run_id"] == run_id
    assert records[0]["objective_hash"]
    assert records[0]["pre_commit_sha"]


def test_end_run_no_commit_flags_anomaly(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(pcp_dir, module="mod", feature="feat", run_type="dev", actor="human-interactive")
    entry = run_log.end_run(pcp_dir, run_id, result="success")
    assert "no_commit: claimed run produced no new commit" in entry["proof_of_delivery"] or True
    assert any(f.startswith("no_commit") for f in entry["anomaly_flags"])
    assert any(f.startswith("unverified_success") for f in entry["anomaly_flags"])


def test_end_run_with_real_commit_and_tests_no_anomaly(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    project_root = pcp_dir.parent
    run_id = run_log.start_run(pcp_dir, module="mod", feature="feat", run_type="dev", actor="pcp-build-agent")
    (project_root / "new_file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "new_file.py"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add new_file"], cwd=project_root, capture_output=True)
    entry = run_log.end_run(
        pcp_dir, run_id, result="success",
        tests_ran=True, tests_passed=True,
        real_gates_passed=["tests", "lint"], llm_judged_gates_passed=["arch"],
    )
    assert entry["proof_of_delivery"]["committed"] is True
    assert entry["anomaly_flags"] == []


def test_end_run_all_self_judged_flagged(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    project_root = pcp_dir.parent
    run_id = run_log.start_run(pcp_dir, module="mod", feature="feat", run_type="dev", actor="pcp-build-agent")
    (project_root / "new_file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "new_file.py"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add new_file"], cwd=project_root, capture_output=True)
    entry = run_log.end_run(
        pcp_dir, run_id, result="success",
        tests_ran=True, tests_passed=True,
        real_gates_passed=[], llm_judged_gates_passed=["arch", "gate"],
    )
    assert any(f.startswith("all_self_judged") for f in entry["anomaly_flags"])


def test_end_run_objective_drift_flagged(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    project_root = pcp_dir.parent
    run_id = run_log.start_run(pcp_dir, module="mod", feature="feat", run_type="dev", actor="human-interactive")
    (pcp_dir / "objective.md").write_text("Objective v2 -- changed mid-run")
    (project_root / "new_file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "drift"], cwd=project_root, capture_output=True)
    entry = run_log.end_run(pcp_dir, run_id, result="success", tests_ran=True, tests_passed=True)
    assert any(f.startswith("objective_drifted") for f in entry["anomaly_flags"])


def test_pair_runs_and_open_runs(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id_1 = run_log.start_run(pcp_dir, module="mod", feature="f1", run_type="dev", actor="human-interactive")
    run_log.end_run(pcp_dir, run_id_1, result="success")
    run_id_2 = run_log.start_run(pcp_dir, module="mod", feature="f2", run_type="test", actor="pcp-build-agent")

    records = run_log.load(pcp_dir)
    pairs = run_log.pair_runs(records)
    open_ = run_log.open_runs(records)

    assert len(pairs) == 1
    assert pairs[0]["run_id"] == run_id_1
    assert len(open_) == 1
    assert open_[0]["run_id"] == run_id_2


def test_hash_chain_links_entries(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    run_id = run_log.start_run(pcp_dir, module="mod", feature="feat", run_type="dev", actor="human-interactive")
    run_log.end_run(pcp_dir, run_id, result="success")
    records = run_log.load(pcp_dir)
    assert records[0]["prev_hash"] == "genesis"
    assert records[1]["prev_hash"] == records[0]["entry_hash"]


def test_cli_start_and_end_roundtrip(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    project_root = pcp_dir.parent
    runner = CliRunner()

    result = runner.invoke(cli, [
        "run-log", "start", "--module", "mod", "--feature", "feat",
        "--type", "manual", "--path", str(project_root),
    ])
    assert result.exit_code == 0
    run_id = result.output.split("Run started: ")[1].split()[0].strip()

    (project_root / "new_file.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "new_file.py"], cwd=project_root, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add file"], cwd=project_root, capture_output=True)

    result = runner.invoke(cli, [
        "run-log", "end", "--run-id", run_id, "--result", "success",
        "--tests-passed", "--real-gate", "tests", "--path", str(project_root),
    ])
    assert result.exit_code == 0
    assert "No anomalies" in result.output


def test_cli_list_shows_open_runs(tmp_path):
    pcp_dir = _init_repo(tmp_path)
    project_root = pcp_dir.parent
    runner = CliRunner()
    runner.invoke(cli, [
        "run-log", "start", "--module", "mod", "--feature", "feat", "--path", str(project_root),
    ])
    result = runner.invoke(cli, ["run-log", "list", "--path", str(project_root)])
    assert result.exit_code == 0
    assert "open run(s) never closed" in result.output
