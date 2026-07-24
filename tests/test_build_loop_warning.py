"""Real-time build-loop warning hook (2026-07-24): PreToolUse companion to
CTRL-037 that fires the moment an Edit/Write happens outside pcp build's
gated loop, instead of only catching it in retrospect via pcp doctor."""

import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from pcp.cli import cli


def _hook_path(tmp_path: Path) -> Path:
    """Scaffold via real `pcp init` so the test exercises the actual
    template, not a hand-copied string."""
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    return tmp_path / ".pcp" / "hooks" / "build_loop_warning.py"


def _run_hook(hook_path: Path, cwd: Path, env: dict, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(hook_path)], input=json.dumps(payload), text=True,
        capture_output=True, cwd=cwd, env=env,
    )


def test_init_scaffolds_build_loop_warning_hook(tmp_path):
    hook_path = _hook_path(tmp_path)
    assert hook_path.exists()
    assert "PreToolUse" in hook_path.read_text()


def test_no_pcp_dir_silent(tmp_path):
    hook_path = _hook_path(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    result = _run_hook(hook_path, other, {"PATH": "/usr/bin:/bin"}, {"cwd": str(other), "session_id": "s1"})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_inside_pcp_build_agent_session_silent(tmp_path):
    hook_path = _hook_path(tmp_path)
    result = _run_hook(
        hook_path, tmp_path, {"PATH": "/usr/bin:/bin", "PCP_AGENT_SESSION": "1"},
        {"cwd": str(tmp_path), "session_id": "s1"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_ad_hoc_edit_warns_once(tmp_path):
    hook_path = _hook_path(tmp_path)
    env = {"PATH": "/usr/bin:/bin"}
    payload = {"cwd": str(tmp_path), "session_id": "s1"}

    first = _run_hook(hook_path, tmp_path, env, payload)
    assert first.returncode == 0
    out = json.loads(first.stdout)
    assert "systemMessage" in out
    assert "pcp build" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out["hookSpecificOutput"]

    second = _run_hook(hook_path, tmp_path, env, payload)
    assert second.returncode == 0
    assert second.stdout.strip() == ""


def test_different_sessions_each_warn_once(tmp_path):
    hook_path = _hook_path(tmp_path)
    env = {"PATH": "/usr/bin:/bin"}

    r1 = _run_hook(hook_path, tmp_path, env, {"cwd": str(tmp_path), "session_id": "s1"})
    r2 = _run_hook(hook_path, tmp_path, env, {"cwd": str(tmp_path), "session_id": "s2"})
    assert r1.stdout.strip() != ""
    assert r2.stdout.strip() != ""


def test_malformed_stdin_falls_through_silently(tmp_path):
    hook_path = _hook_path(tmp_path)
    result = subprocess.run(
        [sys.executable, str(hook_path)], input="not json", text=True,
        capture_output=True, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
