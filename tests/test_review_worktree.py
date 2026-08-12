"""_setup_review_worktree/_cleanup_review_worktree (2026-08-09) -- dedicated,
disposable worktree helpers for review-only agent sessions (CTRL-041,
CTRL-043). Deliberately separate from _setup_worktree/_cleanup_worktree
(those are for build work meant to merge, named feat/{module} branches
reused across runs -- wrong semantics for "throwaway, deleted every time").

Real git plumbing, not mocked -- these tests exercise the actual mechanism
the retrofit depends on for correctness (a fresh detached worktree must see
the criterion's just-committed changes)."""

import subprocess

from pcp.commands.build import _setup_review_worktree, _cleanup_review_worktree


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_setup_review_worktree_creates_detached_worktree_at_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt = _setup_review_worktree(repo)
    try:
        assert wt is not None
        assert wt.exists()
        assert (wt / "README.md").read_text() == "hello\n"
        head = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=wt, capture_output=True, text=True)
        assert head.stdout.strip() == "true"
    finally:
        _cleanup_review_worktree(repo, wt)


def test_setup_review_worktree_sees_just_committed_changes(tmp_path):
    """The exact invariant _run_adversarial_review's retrofit depends on:
    a fresh detached worktree from HEAD must include whatever the primary
    build agent just committed, not stale state."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "new_file.py").write_text("def compute(): return 42\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "criterion A001"], cwd=repo, check=True)

    wt = _setup_review_worktree(repo)
    try:
        assert (wt / "new_file.py").read_text() == "def compute(): return 42\n"
    finally:
        _cleanup_review_worktree(repo, wt)


def test_setup_review_worktree_is_unique_per_call_not_reused(tmp_path):
    """Unlike _setup_worktree (module-branch, reused across runs), a review
    worktree must never be reused -- reuse could leak a prior review
    session's state into a new one."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt1 = _setup_review_worktree(repo)
    wt2 = _setup_review_worktree(repo)
    try:
        assert wt1 != wt2
    finally:
        _cleanup_review_worktree(repo, wt1)
        _cleanup_review_worktree(repo, wt2)


def test_cleanup_review_worktree_removes_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt = _setup_review_worktree(repo)
    assert wt.exists()
    _cleanup_review_worktree(repo, wt)
    assert not wt.exists()

    result = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert str(wt) not in result.stdout


def test_setup_review_worktree_fails_open_on_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    wt = _setup_review_worktree(not_a_repo)
    assert wt is None


def test_editing_review_worktree_does_not_touch_real_repo(tmp_path):
    """The actual safety property this whole mechanism exists for: a
    reviewer that edits/deletes files only ever touches the disposable
    copy, never the real checkout."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wt = _setup_review_worktree(repo)
    try:
        (wt / "README.md").write_text("MALICIOUS EDIT\n")
        (wt / "README.md").unlink()
        assert (repo / "README.md").read_text() == "hello\n"
    finally:
        _cleanup_review_worktree(repo, wt)
