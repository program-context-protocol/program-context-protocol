"""PCP's own git hooks were dead in 6 of 8 local projects and nothing said so.

`pcp init` writes commit-msg + post-commit into .git/hooks/. If core.hooksPath
points elsewhere, git never reads that directory — the files are present,
executable, and inert. Layer 1's commit-msg gate and the post-commit `pcp scan`
both stop firing, so current_state.md ages silently. agentberg: generated
2026-07-24, then 26 more commits, never regenerated.
"""
import subprocess

import pytest

from pcp.commands.doctor import check_git_hooks_reachable


@pytest.fixture(autouse=True)
def _isolate_global_git_config(tmp_path, monkeypatch):
    """This machine's own global config sets core.hooksPath, so a fresh `git init`
    inherits it and the "default is fine" case fails against reality rather than
    against the code. That inheritance is itself the finding — every new repo here
    starts with PCP's hooks shadowed — but the unit test has to be isolated to
    assert anything."""
    empty = tmp_path / "gitconfig-global"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))


def _repo(tmp_path, hooks=("commit-msg", "post-commit")):
    r = tmp_path / "r"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    for h in hooks:
        p = r / ".git" / "hooks" / h
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    return r


def test_default_hooks_path_is_fine(tmp_path):
    assert check_git_hooks_reachable(_repo(tmp_path)) is None


def test_hooks_path_set_to_the_default_dir_is_also_fine(tmp_path):
    """`core.hooksPath = .git/hooks` resolves to where git looks anyway — two of
    the eight projects had exactly this and were wrongly flagged by a first pass."""
    r = _repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"], cwd=r, check=True)
    assert check_git_hooks_reachable(r) is None


def test_absolute_hooks_path_to_the_same_dir_is_fine(tmp_path):
    r = _repo(tmp_path)
    subprocess.run(["git", "config", "core.hooksPath", str(r / ".git" / "hooks")],
                   cwd=r, check=True)
    assert check_git_hooks_reachable(r) is None


def test_hooks_path_elsewhere_reports_both_hooks_unreachable(tmp_path):
    """The real fleet case: a global hooks dir containing no PCP hooks."""
    r = _repo(tmp_path)
    other = tmp_path / "global-hooks"
    other.mkdir()
    subprocess.run(["git", "config", "core.hooksPath", str(other)], cwd=r, check=True)
    out = check_git_hooks_reachable(r)
    assert out is not None
    assert set(out["unreachable"]) == {"commit-msg", "post-commit"}
    assert out["hooks_path"] == str(other)


def test_a_same_named_hook_elsewhere_is_a_replacement_not_a_rescue(tmp_path):
    """The global dir had commit-msg but no post-commit — report the gap precisely."""
    r = _repo(tmp_path)
    other = tmp_path / "global-hooks"
    other.mkdir()
    (other / "commit-msg").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "config", "core.hooksPath", str(other)], cwd=r, check=True)
    out = check_git_hooks_reachable(r)
    # BOTH are unreachable — a same-named file does not rescue PCP's hook, it
    # replaces it, and PCP's commit-msg carries the bypass capture + trailer strip.
    assert set(out["unreachable"]) == {"commit-msg", "post-commit"}
    assert out["replaced_by_other_file"] == ["commit-msg"]


def test_no_pcp_hooks_installed_means_nothing_to_report(tmp_path):
    r = _repo(tmp_path, hooks=())
    subprocess.run(["git", "config", "core.hooksPath", str(tmp_path / "elsewhere")],
                   cwd=r, check=True)
    assert check_git_hooks_reachable(r) is None


def test_non_git_directory_is_silent(tmp_path):
    assert check_git_hooks_reachable(tmp_path) is None


def test_reporter_fires_and_is_shared_by_both_entry_points(tmp_path, capsys):
    """The check must not live in only one entry point — that would recreate the
    silent gap it exists to catch."""
    from pcp.commands.doctor import report_dead_git_hooks
    r = _repo(tmp_path)
    other = tmp_path / "global-hooks"
    other.mkdir()
    (other / "commit-msg").write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "config", "core.hooksPath", str(other)], cwd=r, check=True)

    assert report_dead_git_hooks(r) is True
    out = capsys.readouterr().out
    assert "unreachable" in out
    assert "DIFFERENT file" in out
    assert "GLOBAL core.hooksPath is inherited" in out   # why new projects start broken


def test_reporter_is_silent_when_hooks_are_fine(tmp_path, capsys):
    from pcp.commands.doctor import report_dead_git_hooks
    assert report_dead_git_hooks(_repo(tmp_path)) is False
    assert capsys.readouterr().out == ""
