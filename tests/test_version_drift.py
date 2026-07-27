"""PCP checking whether PCP is current.

Fixes reach frozen-wheel installs only when a human remembers to roll them, and
nothing enumerated those installs. That gap surfaced four times on 2026-07-27:
a Railway-served wheel distributed a version containing a daily unverified
remote overwrite of an agent instruction file for two days after the fix
landed, and two abandoned build worktrees held their own .venv at 0.8.6 with
the same vulnerability — both missed by a rollout working from a remembered
project list instead of the disk.
"""

from unittest.mock import patch

import pytest

from pcp import version_drift as vd


@pytest.fixture
def src(tmp_path):
    def _make(version):
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "program-context-protocol"\nversion = "{version}"\n'
        )
        return tmp_path
    return _make


def _check(monkeypatch, root, installed, editable):
    monkeypatch.setenv("PCP_SOURCE_ROOT", str(root))
    with patch.object(vd, "installed_version", return_value=installed), \
         patch.object(vd, "is_editable", return_value=editable):
        return vd.check()


def test_wheel_behind_source_is_reported_as_genuinely_behind(monkeypatch, src):
    """The motivating case: a worktree .venv frozen at 0.8.6 while source is
    thirteen releases ahead. A wheel's recorded version IS its code."""
    r = _check(monkeypatch, src("0.9.13"), installed="0.8.6", editable=False)
    assert r["status"] == "behind"
    assert r["editable"] is False
    assert "0.8.6" in r["message"] and "0.9.13" in r["message"]
    assert "WHEEL" in r["message"]


def test_editable_behind_source_is_only_a_stale_version_string(monkeypatch, src):
    """The single most repeated mistake in this project's history: treating a
    stale `pcp --version` as proof of stale BEHAVIOUR. Editable installs run
    source live — the code is already current."""
    r = _check(monkeypatch, src("0.9.13"), installed="0.9.6", editable=True)
    assert r["status"] == "stale_metadata"
    assert r["editable"] is True
    assert "EDITABLE" in r["message"]


def test_matching_versions_are_silent(monkeypatch, src):
    r = _check(monkeypatch, src("0.9.13"), installed="0.9.13", editable=True)
    assert r["status"] == "ok"


def test_no_source_root_says_cannot_verify_not_fine(monkeypatch):
    """An unset override must never read as 'verified current' — that is the
    failure mode being fixed, not a state to report as healthy."""
    monkeypatch.delenv("PCP_SOURCE_ROOT", raising=False)
    with patch.object(vd, "installed_version", return_value="0.8.6"), \
         patch.object(vd, "source_root", return_value=None):
        r = vd.check()
    assert r["status"] == "unknown"
    assert "cannot verify" in r["message"]
    assert "PCP_SOURCE_ROOT" in r["message"]


def test_bad_source_root_does_not_crash(monkeypatch):
    monkeypatch.setenv("PCP_SOURCE_ROOT", "/nonexistent/path/xyz")
    with patch.object(vd, "installed_version", return_value="0.9.13"):
        assert vd.check()["status"] == "unknown"


def test_missing_distribution_metadata_is_unknown(monkeypatch):
    with patch.object(vd, "installed_version", return_value=None):
        assert vd.check()["status"] == "unknown"


def test_version_comparison_handles_multi_digit_components(monkeypatch, src):
    """0.9.9 vs 0.9.13 — string comparison would call 0.9.9 newer."""
    r = _check(monkeypatch, src("0.9.13"), installed="0.9.9", editable=False)
    assert r["status"] == "behind", "0.9.9 must be recognised as older than 0.9.13"


def test_doctor_surfaces_drift_without_being_fatal():
    """Advisory by design: a project may legitimately pin an older PCP, and a
    tool that hard-blocks on its own version is worse than the drift."""
    import inspect
    from pcp.commands import doctor
    src_txt = inspect.getsource(doctor)
    assert "version_drift" in src_txt
    assert "PCP version drift" in src_txt
    # must not exit on drift
    idx = src_txt.index("version_drift")
    assert "sys.exit" not in src_txt[idx:idx + 900]
