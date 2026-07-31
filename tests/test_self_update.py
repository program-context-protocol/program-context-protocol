import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pcp.cli import cli


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "test@localhost"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)


def test_self_update_refuses_when_not_a_git_checkout(tmp_path):
    with patch("pcp.version_drift.source_root", return_value=None):
        result = CliRunner().invoke(cli, ["self-update"])
    assert result.exit_code != 0
    assert "No git checkout" in result.output


def test_self_update_refuses_with_uncommitted_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo, "1.0.0")
    (repo / "dirty.txt").write_text("uncommitted")

    with patch("pcp.version_drift.source_root", return_value=repo):
        result = CliRunner().invoke(cli, ["self-update"])
    assert result.exit_code != 0
    assert "uncommitted changes" in result.output


def test_self_update_pulls_fast_forward_and_reports_version_change(tmp_path):
    remote = tmp_path / "remote"
    _init_repo(remote, "1.0.0")
    _git(["config", "receive.denyCurrentBranch", "ignore"], remote)

    local = tmp_path / "local"
    _git(["clone", "-q", str(remote), str(local)], tmp_path)
    _git(["config", "user.email", "test@localhost"], local)
    _git(["config", "user.name", "Test"], local)

    # Simulate a new release landing on origin.
    (remote / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.1.0"\n')
    _git(["add", "-A"], remote)
    _git(["commit", "-q", "-m", "bump"], remote)

    with patch("pcp.version_drift.source_root", return_value=local), \
         patch("pcp.version_drift.is_editable", return_value=True):
        result = CliRunner().invoke(cli, ["self-update"])

    assert result.exit_code == 0
    assert "1.0.0 -> 1.1.0" in result.output
    assert (local / "pyproject.toml").read_text() == (remote / "pyproject.toml").read_text()


def test_self_update_reports_already_up_to_date(tmp_path):
    remote = tmp_path / "remote"
    _init_repo(remote, "1.0.0")
    _git(["config", "receive.denyCurrentBranch", "ignore"], remote)

    local = tmp_path / "local"
    _git(["clone", "-q", str(remote), str(local)], tmp_path)

    with patch("pcp.version_drift.source_root", return_value=local), \
         patch("pcp.version_drift.is_editable", return_value=True):
        result = CliRunner().invoke(cli, ["self-update"])

    assert result.exit_code == 0
    assert "Already up to date" in result.output


def test_self_update_wheel_install_tells_user_to_reinstall(tmp_path):
    remote = tmp_path / "remote"
    _init_repo(remote, "1.0.0")
    _git(["config", "receive.denyCurrentBranch", "ignore"], remote)

    local = tmp_path / "local"
    _git(["clone", "-q", str(remote), str(local)], tmp_path)

    (remote / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.0"\n')
    _git(["add", "-A"], remote)
    _git(["commit", "-q", "-m", "bump"], remote)

    with patch("pcp.version_drift.source_root", return_value=local), \
         patch("pcp.version_drift.is_editable", return_value=False):
        result = CliRunner().invoke(cli, ["self-update"])

    assert result.exit_code == 0
    assert "wheel install" in result.output
    assert "pip install -e" in result.output


def test_self_update_check_reports_current(tmp_path):
    remote = tmp_path / "remote"
    _init_repo(remote, "1.0.0")

    local = tmp_path / "local"
    _git(["clone", "-q", str(remote), str(local)], tmp_path)

    with patch("pcp.version_drift.source_root", return_value=local):
        result = CliRunner().invoke(cli, ["self-update", "--check"])

    assert result.exit_code == 0
    assert "up to date" in result.output
    # --check must never modify the working tree.
    assert (local / "pyproject.toml").read_text() == '[project]\nname = "x"\nversion = "1.0.0"\n'


def test_self_update_check_reports_available_without_pulling(tmp_path):
    remote = tmp_path / "remote"
    _init_repo(remote, "1.0.0")

    local = tmp_path / "local"
    _git(["clone", "-q", str(remote), str(local)], tmp_path)

    (remote / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "2.0.0"\n')
    _git(["add", "-A"], remote)
    _git(["commit", "-q", "-m", "bump"], remote)

    with patch("pcp.version_drift.source_root", return_value=local):
        result = CliRunner().invoke(cli, ["self-update", "--check"])

    assert result.exit_code == 0
    assert "update available" in result.output.lower()
    # Still 1.0.0 -- --check must not pull.
    assert (local / "pyproject.toml").read_text() == '[project]\nname = "x"\nversion = "1.0.0"\n'


def test_self_update_check_refuses_when_not_a_git_checkout():
    with patch("pcp.version_drift.source_root", return_value=None):
        result = CliRunner().invoke(cli, ["self-update", "--check"])
    assert result.exit_code != 0


def test_init_scaffolds_session_update_check_hook(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    hook = tmp_path / ".pcp" / "hooks" / "session_update_check.py"
    assert hook.exists()
    text = hook.read_text()
    assert '"pcp", "self-update", "--check"' in text
    assert "NOT wired automatically" in text
    assert "SessionStart" in text
