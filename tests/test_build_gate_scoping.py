"""Dogfood-found gate-input fixes (2026-07-17): PCP-operational file exclusion
and criterion-scoped judge framing."""

import subprocess
from unittest.mock import patch

from pcp.commands.build import (
    _criterion_scope_framing,
    _get_changed_files_since,
    _get_working_diff,
    _is_pcp_operational,
    _run_gate_check,
)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()


def test_committed_agent_work_is_visible(tmp_path):
    """Dogfood round 2: agent committed a 65-line implementation and the old
    staged+unstaged view reported 'No files were modified'. Committed work
    since the criterion-start ref must count."""
    start = _repo(tmp_path)
    (tmp_path / "impl.py").write_text("def f():\n    return 42\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "agent work, committed")
    files = _get_changed_files_since(tmp_path, start)
    assert files == ["impl.py"]
    diff = _get_working_diff(tmp_path, start)
    assert "return 42" in diff


def test_uncommitted_and_untracked_work_still_visible(tmp_path):
    start = _repo(tmp_path)
    (tmp_path / "base.py").write_text("x = 2\n")          # unstaged edit
    (tmp_path / "new_untracked.py").write_text("y = 3\n")  # never git-added
    files = _get_changed_files_since(tmp_path, start)
    assert "base.py" in files
    assert "new_untracked.py" in files


def test_operational_paths_detected():
    assert _is_pcp_operational(".pcp/token_ledger.yaml")
    assert _is_pcp_operational(".pcp/telemetry.jsonl")
    assert _is_pcp_operational(".pcp/evidence/auth/A1/attempt_1/gate.txt")
    assert _is_pcp_operational(".pcp/transcripts/abc.jsonl.gz")
    assert _is_pcp_operational("./.pcp/token_ledger.yaml")


def test_agent_deliverables_not_operational():
    assert not _is_pcp_operational("src/app.py")
    assert not _is_pcp_operational(".pcp/strategy/modules/auth/acceptance.yaml")
    assert not _is_pcp_operational(".pcp/design_system.md")
    assert not _is_pcp_operational(".pcp/objective.md")


def test_framing_names_criterion_and_partial_build():
    ctx = {"module": "storage", "criterion_id": "A001",
           "criterion_description": "JSON persistence layer", "attempt": 1, "files": []}
    framing = _criterion_scope_framing(ctx)
    assert "A001" in framing
    assert "JSON persistence layer" in framing
    assert "NOT built yet" in framing
    assert "NOT a regression" in framing


def test_gate_check_prompt_carries_framing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("objective")
    ctx = {"module": "storage", "criterion_id": "A001",
           "criterion_description": "JSON persistence layer", "attempt": 1, "files": []}
    captured = {}

    def fake_call_json(system, prompt, **kw):
        captured["prompt"] = prompt
        return {"recommendation": "merge", "alignment_score": 1.0}, {}

    with patch("pcp.llm.client.call_json", side_effect=fake_call_json):
        issues = _run_gate_check(pcp_dir, "diff content", ctx)
    assert issues == []
    assert "ONE acceptance criterion" in captured["prompt"]
    assert "A001" in captured["prompt"]
