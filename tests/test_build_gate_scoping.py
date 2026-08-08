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
    _snapshot_dirty_file_hashes,
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


# ── stale pre-existing dirty state (win2mac dogfood, 2026-08-08) ──
# A worktree reused from an interrupted prior run keeps its dirty state by
# design (_sync_worktree_to_base won't touch a dirty tree) -- without
# exclude_dirty, that leftover state leaks into a DIFFERENT criterion's
# gate scope even though this criterion's own agent never touched it.

def test_pre_existing_dirty_file_excluded_when_untouched(tmp_path):
    start = _repo(tmp_path)
    (tmp_path / "stale.py").write_text("leftover = 1\n")  # dirty BEFORE this "criterion" starts
    pre_existing = _snapshot_dirty_file_hashes(tmp_path, {"stale.py"})

    files = _get_changed_files_since(tmp_path, start, exclude_dirty=pre_existing)
    assert files == []
    diff = _get_working_diff(tmp_path, start, exclude_dirty=pre_existing)
    assert "leftover" not in diff


def test_pre_existing_dirty_file_still_counted_once_actually_touched(tmp_path):
    """Content-hash, not path-only: excluding the SNAPSHOT must not exclude
    real new work on that same path -- if this criterion's own agent edits
    it further (even a path that happened to already be dirty), that's
    genuine new content and must still reach the gates."""
    start = _repo(tmp_path)
    (tmp_path / "stale.py").write_text("leftover = 1\n")
    pre_existing = _snapshot_dirty_file_hashes(tmp_path, {"stale.py"})  # hash of the ORIGINAL content
    (tmp_path / "stale.py").write_text("leftover = 1\nreal_new_work = 2\n")  # content moved on

    files = _get_changed_files_since(tmp_path, start, exclude_dirty=pre_existing)
    assert files == ["stale.py"]
    diff = _get_working_diff(tmp_path, start, exclude_dirty=pre_existing)
    assert "real_new_work" in diff


def test_committed_work_never_excluded_by_pre_existing_dirty(tmp_path):
    """exclude_dirty only applies to the staged/unstaged/untracked buckets --
    anything the agent actually COMMITTED during this attempt is unambiguous
    real work and must never be excluded, even if the same path happened to
    be dirty before this criterion started."""
    start = _repo(tmp_path)
    (tmp_path / "stale.py").write_text("leftover = 1\n")
    pre_existing = _snapshot_dirty_file_hashes(tmp_path, {"stale.py"})
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "agent committed real work over the stale file")

    files = _get_changed_files_since(tmp_path, start, exclude_dirty=pre_existing)
    assert files == ["stale.py"]


def test_content_hash_helpers(tmp_path):
    from pcp.commands.build import _content_hash, _unchanged_since_snapshot
    _repo(tmp_path)
    (tmp_path / "f.py").write_text("a = 1\n")
    h1 = _content_hash(tmp_path, "f.py")
    assert h1 is not None
    assert _content_hash(tmp_path, "does_not_exist.py") is None

    snapshot = {"f.py": h1}
    assert _unchanged_since_snapshot(tmp_path, "f.py", snapshot) is True
    (tmp_path / "f.py").write_text("a = 2\n")
    assert _unchanged_since_snapshot(tmp_path, "f.py", snapshot) is False
    assert _unchanged_since_snapshot(tmp_path, "not_in_snapshot.py", snapshot) is False


def test_pre_existing_untracked_file_excluded_from_diff_synthesis(tmp_path):
    """The untracked-file diff-synthesis loop (git diff never shows untracked
    files on its own) must also respect exclude_dirty, not just the tracked
    `git diff` pathspec exclusion."""
    start = _repo(tmp_path)
    (tmp_path / "stale_untracked.py").write_text("leftover_untracked = 1\n")
    pre_existing = _snapshot_dirty_file_hashes(tmp_path, {"stale_untracked.py"})

    diff = _get_working_diff(tmp_path, start, exclude_dirty=pre_existing)
    assert "leftover_untracked" not in diff


def test_operational_paths_detected():
    assert _is_pcp_operational(".pcp/token_ledger.yaml")
    assert _is_pcp_operational(".pcp/telemetry.jsonl")
    assert _is_pcp_operational(".pcp/evidence/auth/A1/attempt_1/gate.txt")
    assert _is_pcp_operational(".pcp/transcripts/abc.jsonl.gz")
    assert _is_pcp_operational("./.pcp/token_ledger.yaml")


def test_agent_local_config_now_also_operational():
    """Real gap closed 2026-08-08: .testmondata is seeded (untracked) into
    every fresh worktree and never cleaned up, so it sat in changed_files/
    the working diff for every criterion indefinitely -- unlike
    _PCP_OPERATIONAL_PATHS, _AGENT_LOCAL_CONFIG was excluded from commit
    staging but never from what gates see."""
    assert _is_pcp_operational(".testmondata")
    assert _is_pcp_operational(".testmondata-journal")
    assert _is_pcp_operational(".claude/settings.json")
    assert _is_pcp_operational(".claude/settings.local.json")


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
