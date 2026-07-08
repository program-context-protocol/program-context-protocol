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
    with patch("pcp.commands.install_hook._install_cron_scripts"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])
    assert result.exit_code == 2
    assert "not a git repository" in result.output


def test_install_hook_writes_commit_msg_hook(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._install_cron_scripts") as mock_cron:
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
    with patch("pcp.commands.install_hook._install_cron_scripts"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "already exists" in result.output
    assert hook_path.read_text() == "#!/bin/sh\necho existing\n"


def test_install_hook_force_overwrites(tmp_path):
    _init_git_repo(tmp_path)
    hook_path = tmp_path / ".git" / "hooks" / "commit-msg"
    hook_path.parent.mkdir(exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho existing\n")
    with patch("pcp.commands.install_hook._install_cron_scripts"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--force"])
    assert result.exit_code == 0
    assert "pcp check --commit-msg-file" in hook_path.read_text()


def test_install_hook_pre_commit_framework_creates_config(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._install_cron_scripts"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--pre-commit-framework"])
    assert result.exit_code == 0
    config = (tmp_path / ".pre-commit-config.yaml").read_text()
    assert "pcp-check" in config


def test_install_hook_pre_commit_framework_skips_if_already_present(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos:\n  - repo: local\n    hooks:\n      - id: pcp-check\n")
    with patch("pcp.commands.install_hook._install_cron_scripts"):
        runner = CliRunner()
        result = runner.invoke(cli, ["install-hook", "--path", str(tmp_path), "--pre-commit-framework"])
    assert result.exit_code == 0
    assert "already in" in result.output


# ── install_git_hook() -- the pure, no-cron-side-effect function pcp init calls ──

def test_install_git_hook_writes_hook_no_cron(tmp_path):
    _init_git_repo(tmp_path)
    with patch("pcp.commands.install_hook._install_cron_scripts") as mock_cron:
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
