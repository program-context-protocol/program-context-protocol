"""Objective-conflict gate (CTRL-035) -- unit + CLI coverage.

Covers: objective_conflicts.py's deterministic hash/reconcile/dismiss logic,
capture.py stamping objective_hash_at_flag when drift_flag is set, `pcp
build`'s hard-block preflight, `pcp objective-conflicts`, and `pcp
correct-objective`'s propose-diff-approve-write flow.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp import capture, objective_conflicts


def _pcp_dir(tmp_path) -> Path:
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nStore instances forever.")
    (pcp_dir / "target_state.md").write_text("# Target\nA versioned store is the source of truth.")
    return pcp_dir


def _flagged_item(pcp_dir: Path, item_id="BRD-001", extra=None) -> dict:
    item = {
        "id": item_id, "description": "Stop storing business-transaction instances",
        "status": "active", "superseded_by": None, "drift_flag": "conflicts with target_state.md's versioned-store language",
        "objective_hash_at_flag": objective_conflicts.objective_hash(pcp_dir),
    }
    if extra:
        item.update(extra)
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": [item]}))
    return item


# ── objective_conflicts.py ──

def test_objective_hash_deterministic_and_content_sensitive(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    h1 = objective_conflicts.objective_hash(pcp_dir)
    h2 = objective_conflicts.objective_hash(pcp_dir)
    assert h1 == h2
    (pcp_dir / "objective.md").write_text("# Objective\nSomething else entirely.")
    assert objective_conflicts.objective_hash(pcp_dir) != h1


def test_reconcile_unresolved_when_hash_still_matches(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    unresolved = objective_conflicts.reconcile(pcp_dir)
    assert len(unresolved) == 1
    assert unresolved[0]["id"] == "BRD-001"


def test_reconcile_auto_clears_when_objective_edited(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    (pcp_dir / "objective.md").write_text("# Objective\nDoes NOT store business-transaction instances.")
    unresolved = objective_conflicts.reconcile(pcp_dir)
    assert unresolved == []
    items = yaml.safe_load((pcp_dir / "brd_items.yaml").read_text())["items"]
    assert items[0]["drift_resolved_at"] is not None


def test_reconcile_treats_missing_hash_as_unresolved_fail_loud(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    item = {
        "id": "BRD-002", "description": "old-style flag", "status": "active",
        "drift_flag": "predates objective_hash_at_flag", "objective_hash_at_flag": None,
    }
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": [item]}))
    unresolved = objective_conflicts.reconcile(pcp_dir)
    assert len(unresolved) == 1


def test_reconcile_ignores_non_flagged_and_superseded_items(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    items = [
        {"id": "BRD-001", "status": "active", "drift_flag": None},
        {"id": "BRD-002", "status": "superseded", "drift_flag": "conflict text", "objective_hash_at_flag": "deadbeef"},
    ]
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": items}))
    assert objective_conflicts.reconcile(pcp_dir) == []


def test_dismiss_requires_reason(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    try:
        objective_conflicts.dismiss(pcp_dir, "BRD-001", "")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_dismiss_clears_conflict(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    found = objective_conflicts.dismiss(pcp_dir, "BRD-001", "false positive, module-level not objective-level")
    assert found
    assert objective_conflicts.reconcile(pcp_dir) == []


def test_dismiss_returns_false_for_unknown_id(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    assert objective_conflicts.dismiss(pcp_dir, "BRD-999", "n/a") is False


# ── capture.py stamping ──

def test_apply_business_items_stamps_hash_when_drift_flagged(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    capture.apply_business_items(
        pcp_dir,
        [{"description": "Stop storing instances", "drift_flag": "conflicts with target_state.md", "supersedes": None}],
        source="session:abc",
    )
    items = yaml.safe_load((pcp_dir / "brd_items.yaml").read_text())["items"]
    assert items[0]["objective_hash_at_flag"] == objective_conflicts.objective_hash(pcp_dir)


def test_apply_business_items_no_hash_when_not_flagged(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    capture.apply_business_items(
        pcp_dir,
        [{"description": "Add a export button", "drift_flag": None, "supersedes": None}],
        source="session:abc",
    )
    items = yaml.safe_load((pcp_dir / "brd_items.yaml").read_text())["items"]
    assert items[0]["objective_hash_at_flag"] is None


# ── pcp build hard-block ──

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _init_repo(tmp_path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "README.md").write_text("init\n")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-q", "-m", "init"], tmp_path)
    return tmp_path


def test_pcp_build_hard_blocks_on_unresolved_conflict(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nStore instances forever.")
    (pcp_dir / "target_state.md").write_text("# Target\nVersioned store is the source of truth.")
    _flagged_item(pcp_dir)

    runner = CliRunner()
    result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 2
    assert "Build blocked" in result.output
    assert "BRD-001" in result.output


def test_pcp_build_proceeds_when_no_conflicts(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    (pcp_dir / "target_state.md").write_text("# Target\nDone state.")
    # No modules dir at all -- proves the gate ran and passed (no "Build blocked"),
    # falling through to the normal "no modules found" path, not the conflict exit(2).
    runner = CliRunner()
    result = runner.invoke(cli, ["build", "--path", str(repo)])
    assert "Build blocked" not in result.output
    assert result.exit_code == 0
    assert "No modules found" in result.output


# ── pcp build self-capture preflight ──

def test_pcp_build_self_captures_live_session_before_gate(tmp_path, monkeypatch):
    """Real incident, 2026-07-22: a correction discussed in the SAME still-open
    session as the 'go ahead' never got classified because SessionEnd hadn't
    fired. `pcp build` must capture its own live session before the CTRL-035
    check even looks at brd_items.yaml, deterministically -- not dependent on
    the /pcp skill remembering to call `pcp capture` itself."""
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    (pcp_dir / "target_state.md").write_text("# Target\nDone state.")

    fake_transcript = tmp_path / "fake_session.jsonl"
    fake_transcript.write_text('{"message": {"role": "user", "content": "hi"}}\n')

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc")
    calls = []

    def _fake_find(session_id):
        calls.append(("find", session_id))
        return fake_transcript

    def _fake_run_capture(pcp_dir_arg, transcript_path, source, session_id=None):
        calls.append(("run_capture", str(transcript_path), source, session_id))
        return {"business_count": 0, "technical_count": 0, "archived_path": None}

    with patch("pcp.commands.build.find_transcript_for_session", side_effect=_fake_find), \
         patch("pcp.commands.build.run_capture", side_effect=_fake_run_capture):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert ("find", "sess-abc") in calls
    assert any(c[0] == "run_capture" and c[3] == "sess-abc" for c in calls)


def test_pcp_build_self_capture_failure_never_blocks_build(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    (pcp_dir / "target_state.md").write_text("# Target\nDone state.")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc")
    with patch("pcp.commands.build.find_transcript_for_session", side_effect=RuntimeError("boom")):
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    assert "Self-capture skipped" in result.output


def test_pcp_build_no_self_capture_without_session_id(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Objective\nBuild things.")
    (pcp_dir / "target_state.md").write_text("# Target\nDone state.")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    with patch("pcp.commands.build.run_capture") as mock_run_capture:
        runner = CliRunner()
        result = runner.invoke(cli, ["build", "--path", str(repo)])

    assert result.exit_code == 0, result.output
    mock_run_capture.assert_not_called()


# ── pcp objective-conflicts CLI ──

def test_objective_conflicts_cmd_lists_unresolved(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    runner = CliRunner()
    result = runner.invoke(cli, ["objective-conflicts", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "BRD-001" in result.output


def test_objective_conflicts_cmd_dismiss_requires_reason(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    runner = CliRunner()
    result = runner.invoke(cli, ["objective-conflicts", "--path", str(tmp_path), "--dismiss", "BRD-001"])
    assert result.exit_code == 2


def test_objective_conflicts_cmd_dismiss_clears(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    _flagged_item(pcp_dir)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "objective-conflicts", "--path", str(tmp_path), "--dismiss", "BRD-001", "--reason", "false positive",
    ])
    assert result.exit_code == 0
    assert "dismissed" in result.output.lower()
    assert objective_conflicts.reconcile(pcp_dir) == []


# ── pcp correct-objective CLI ──

def test_correct_objective_writes_on_approval(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "modules").mkdir()

    mock_rewrite = {
        "objective_md": "# Objective\nDoes NOT store business-transaction instances.",
        "target_state_md": "# Target\nNo instance storage; schema only.",
        "summary": "Removed instance-storage language per business decision.",
    }
    mock_validate = {"coverage_score": 1.0, "gaps": [], "overlaps": [], "missing_modules": []}

    with patch("pcp.llm.client.call_json", side_effect=[mock_rewrite, mock_validate]), \
         patch("click.confirm", return_value=True):
        runner = CliRunner()
        result = runner.invoke(cli, ["correct-objective", "Stop storing instances", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "NOT store" in (pcp_dir / "objective.md").read_text()
    assert "No instance storage" in (pcp_dir / "target_state.md").read_text()


def test_correct_objective_aborts_without_approval_leaves_files_unchanged(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "modules").mkdir()
    original = (pcp_dir / "objective.md").read_text()

    mock_rewrite = {
        "objective_md": "# Objective\nSomething different.",
        "target_state_md": "# Target\nSomething different.",
        "summary": "...",
    }
    with patch("pcp.llm.client.call_json", return_value=mock_rewrite), \
         patch("click.confirm", return_value=False):
        runner = CliRunner()
        result = runner.invoke(cli, ["correct-objective", "Stop storing instances", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert (pcp_dir / "objective.md").read_text() == original


def test_correct_objective_from_conflict_resolves_flagged_item(tmp_path):
    pcp_dir = _pcp_dir(tmp_path)
    (pcp_dir / "strategy").mkdir()
    (pcp_dir / "strategy" / "modules").mkdir()
    _flagged_item(pcp_dir)

    mock_rewrite = {
        "objective_md": "# Objective\nDoes NOT store business-transaction instances.",
        "target_state_md": "# Target\nNo instance storage.",
        "summary": "Removed instance-storage language.",
    }
    mock_validate = {"coverage_score": 1.0, "gaps": [], "overlaps": [], "missing_modules": []}

    with patch("pcp.llm.client.call_json", side_effect=[mock_rewrite, mock_validate]), \
         patch("click.confirm", return_value=True):
        runner = CliRunner()
        result = runner.invoke(cli, ["correct-objective", "--from-conflict", "BRD-001", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert objective_conflicts.reconcile(pcp_dir) == []
