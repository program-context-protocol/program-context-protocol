from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.install_hook import install_git_hook


def _init_git_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return pcp_dir


def test_install_hook_not_a_git_repo_exits_2(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "not a git repository" in result.output


def test_install_hook_writes_commit_msg_hook(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs") as mock_cron:
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])
    assert result.exit_code == 0
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    assert hook_path.exists()
    assert "pcp check --commit-msg-file" in hook_path.read_text()
    assert oct(hook_path.stat().st_mode)[-3:] == "755"
    mock_cron.assert_called_once()


def test_install_hook_refuses_overwrite_without_force(tmp_path):
    _init_git_repo(tmp_path)
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    hook_path.parent.mkdir(exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho existing\n")
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert hook_path.read_text() == "#!/bin/sh\necho existing\n"


def test_install_hook_installs_missing_post_commit_even_when_commit_msg_exists(tmp_path):
    """Bug fixed 2026-07-21: the CLI command used to exit before ever
    checking post-commit whenever commit-msg already existed -- a project
    with a working commit-msg hook but no post-commit hook (exactly PCP's
    own repo, predating the post-commit feature) could never get it
    installed via this command without --force, only via `pcp init`'s
    own install_git_hook() call, which never had this bug."""
    _init_git_repo(tmp_path)
    commit_msg_path = tmp_path / ".git" / "hooks" / "commit-msg"
    commit_msg_path.parent.mkdir(exist_ok=True)
    commit_msg_path.write_text("#!/bin/sh\npcp check --commit-msg-file \"$1\"\n")
    post_commit_path = tmp_path / ".git" / "hooks" / "post-commit"
    assert not post_commit_path.exists()

    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])

    # commit-msg already existing (any content) without --force is still a
    # refusal for THAT hook specifically (exit 1, unchanged) -- same
    # long-standing contract test_install_hook_refuses_overwrite_without_force
    # already covers. The fix is that post-commit no longer gets skipped
    # just because commit-msg's own outcome was a refusal.
    assert result.exit_code == 1
    assert post_commit_path.exists()
    assert "pcp scan" in post_commit_path.read_text()
    # commit-msg itself untouched -- refused, not overwritten.
    assert commit_msg_path.read_text() == "#!/bin/sh\npcp check --commit-msg-file \"$1\"\n"


def test_install_hook_force_overwrites(tmp_path):
    _init_git_repo(tmp_path)
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    hook_path.parent.mkdir(exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho existing\n")
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "pcp check --commit-msg-file" in hook_path.read_text()


def test_install_hook_pre_commit_framework_creates_config(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--pre-commit-framework"])
    assert result.exit_code == 0
    config = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert "pcp-check" in config


def test_install_hook_pre_commit_framework_skips_if_already_present(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos:\n  - repo: local\n    hooks:\n      - id: pcp-check\n")
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--pre-commit-framework"])
    assert result.exit_code == 0
    assert "already in" in result.output


# ── install_git_hook() -- the pure, no-crontab-side-effect function pcp init calls ──

def test_install_git_hook_never_touches_crontab(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._remove_legacy_cron_jobs") as mock_cron:
        installed, msg = install_git_hook(tmp_path)
    assert installed
    assert "installed" in msg
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    assert hook_path.exists()
    mock_cron.assert_not_called()


def test_install_git_hook_returns_false_outside_git_repo(tmp_path):
    installed, msg = install_git_hook(tmp_path)
    assert not installed
    assert "not a git repository" in msg


def test_install_git_hook_idempotent_when_already_installed(tmp_path):
    _init_git_repo(tmp_path)
    install_git_hook(tmp_path)
    installed, msg = install_git_hook(tmp_path)  # second call, no --force
    assert installed
    assert "already installed" in msg


def test_install_git_hook_refuses_to_clobber_a_foreign_hook(tmp_path):
    _init_git_repo(tmp_path)
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    hook_path.parent.mkdir(exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho some other tool's hook\n")
    installed, msg = install_git_hook(tmp_path)
    assert not installed
    assert "already exists" in msg
    assert hook_path.read_text() == "#!/bin/sh\necho some other tool's hook\n"


# ── the installed hook's actual commit-hygiene behavior, not just its presence ──

def test_installed_hook_strips_claude_and_anthropic_coauthor_lines(tmp_path):
    """Defense in depth for 'never attribute a commit to Claude' -- runs the
    real installed hook script (not a re-implementation of its logic) against
    a real commit message file, since the whole point is that this fires even
    if something upstream (a different tool, a habit) added the line."""
    import subprocess

    _init_git_repo(tmp_path)
    install_git_hook(tmp_path)
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"

    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(
        "Fix the parser bug\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "Co-Authored-By: Real Person <real@example.com>\n"
        "Co-authored-by: anthropic-bot <bot@anthropic.com>\n"
    )

    # No `pcp` on PATH -- the stripping step runs before `pcp check` in the
    # template, so its effect is verifiable regardless of pcp check's own
    # (irrelevant here) outcome.
    subprocess.run(["sh", str(hook_path), str(msg_file)], cwd=tmp_path,
                    env={"PATH": "/usr/bin:/bin"})

    result = msg_file.read_text()
    assert "Claude <noreply@anthropic.com>" not in result
    assert "anthropic-bot" not in result
    assert "Real Person <real@example.com>" in result


# ── Legacy cron removal (2026-07-27 pre-launch security fix) ──

def test_pcp_never_installs_cron_jobs_anymore():
    """The installer is gone, not disabled. A daily unverified `curl` that
    overwrites an agent INSTRUCTION file has no safe configuration."""
    from pcp.commands import install_hook as ih
    assert not hasattr(ih, "_install_cron_scripts")
    # Strip comments: the removal is deliberately documented in a comment that
    # names what was deleted, so a naive substring check would match its own
    # explanation. What must be gone is executable code.
    code = "\n".join(
        ln for ln in Path(ih.__file__).read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "raw.githubusercontent.com" not in code
    assert "upgrade_skill.sh" not in code
    assert "aggregate_interventions.sh" not in code


def test_remove_legacy_cron_jobs_strips_only_pcp_lines():
    from pcp.commands.install_hook import _remove_legacy_cron_jobs

    existing = (
        "0 6 * * * /Users/someone/backup.sh\n"
        "47 8 * * * /Users/someone/.pcp/cron/aggregate_interventions.sh >> log 2>&1\n"
        "13 9 * * * /Users/someone/.pcp/cron/upgrade_skill.sh >> log 2>&1\n"
        "30 2 * * * /Users/someone/other-job.sh\n"
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        if cmd == ["crontab", "-l"]:
            return SimpleNamespace(returncode=0, stdout=existing, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        _remove_legacy_cron_jobs()

    written = [inp for cmd, inp in calls if cmd == ["crontab", "-"]]
    assert len(written) == 1, "should rewrite the crontab exactly once"
    result = written[0]
    assert "/.pcp/cron/" not in result
    assert "/Users/someone/backup.sh" in result
    assert "/Users/someone/other-job.sh" in result


def test_remove_legacy_cron_jobs_no_write_when_nothing_to_remove():
    from pcp.commands.install_hook import _remove_legacy_cron_jobs

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["crontab", "-l"]:
            return SimpleNamespace(returncode=0, stdout="0 6 * * * /backup.sh\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        _remove_legacy_cron_jobs()

    assert ["crontab", "-"] not in calls, "must not rewrite an untouched crontab"


def test_remove_legacy_cron_jobs_survives_missing_crontab():
    """No crontab binary (common in containers) must not break hook install."""
    from pcp.commands.install_hook import _remove_legacy_cron_jobs
    with patch("subprocess.run", side_effect=FileNotFoundError):
        _remove_legacy_cron_jobs()  # must not raise
