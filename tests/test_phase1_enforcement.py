"""Phase 1 enforcement-depth items (2026-07-17 build plan)."""

import subprocess

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.coupling import compute_change_coupling
from pcp.commands.docs import _specificity_rank
from pcp.commands.doctor import _rung_tooling_recommendations


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


# ── 1.1 deploy policy scaffold ──

def test_init_scaffolds_deploy_policy(tmp_path):
    CliRunner().invoke(cli, ["init", "--path", str(tmp_path)])
    rego = tmp_path / ".pcp" / "policies" / "deploy_policy.rego"
    assert rego.exists()
    assert "freeze_days" in rego.read_text()


# ── 1.2 rung tooling recommendations ──

def _project_with_tier(tmp_path, tier):
    pcp_dir = tmp_path / ".pcp"
    mod = pcp_dir / "strategy" / "modules" / "m"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "criteria": [{"id": "A1", "description": "x", "logic_tier": tier}]}))
    return pcp_dir


def test_rung6_without_schema_lib_flagged(tmp_path):
    pcp_dir = _project_with_tier(tmp_path, 6)
    recs = _rung_tooling_recommendations(pcp_dir, tmp_path)
    assert any("rung-6" in r for r in recs)


def test_rung6_with_instructor_not_flagged(tmp_path):
    pcp_dir = _project_with_tier(tmp_path, 6)
    (tmp_path / "requirements.txt").write_text("instructor>=1.0\n")
    recs = _rung_tooling_recommendations(pcp_dir, tmp_path)
    assert not any("rung-6" in r for r in recs)


# ── 1.3 change coupling ──

def test_change_coupling_flags_undeclared_cochange(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    for i in range(6):
        (tmp_path / "a.py").write_text(f"a={i}\n")
        (tmp_path / "b.py").write_text(f"b={i}\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", f"c{i}")
    modules = {
        "alpha": {"spec": {"dependencies": []}, "acceptance": {"criteria": [{"id": "A1", "target": "a.py"}]}},
        "beta": {"spec": {"dependencies": []}, "acceptance": {"criteria": [{"id": "B1", "target": "b.py"}]}},
    }
    hidden = compute_change_coupling(tmp_path, modules)
    assert len(hidden) == 1
    assert sorted(hidden[0]["modules"]) == ["alpha", "beta"]


def test_change_coupling_ignores_declared_dependency(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    for i in range(6):
        (tmp_path / "a.py").write_text(f"a={i}\n")
        (tmp_path / "b.py").write_text(f"b={i}\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-m", f"c{i}")
    modules = {
        "alpha": {"spec": {"dependencies": ["beta"]}, "acceptance": {"criteria": [{"id": "A1", "target": "a.py"}]}},
        "beta": {"spec": {"dependencies": []}, "acceptance": {"criteria": [{"id": "B1", "target": "b.py"}]}},
    }
    assert compute_change_coupling(tmp_path, modules) == []


# ── 1.5 diff drift split ──

def test_diff_splits_regression_from_not_yet_built(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "target_state.md").write_text("target")
    # Previous diff.md snapshot says CORE/A1 was complete
    (pcp_dir / "diff.md").write_text(
        "# Diff\n## Completed Snapshot\n\n- CORE/A1: was done\n## Next Actions\n")
    # Now current_state says A1 pending again (regression) + A2 never built
    (pcp_dir / "current_state.md").write_text(
        "- [ ] CORE/A1: was done\n- [ ] CORE/A2: new thing\n")
    result = CliRunner().invoke(cli, ["diff", "--path", str(tmp_path)])
    assert result.exit_code == 0
    text = (pcp_dir / "diff.md").read_text()
    reg_section = text.split("## Regressions")[1].split("## Pending Gaps")[0]
    pending_section = text.split("## Pending Gaps")[1].split("## Completed Snapshot")[0]
    assert "CORE/A1" in reg_section
    assert "CORE/A2" in pending_section
    assert "CORE/A2" not in reg_section


# ── 1.8 specificity ranking ──

def test_specificity_rank_prefers_rare_keyword_match():
    items = [
        {"id": 1, "description": "common word system stuff"},
        {"id": 2, "description": "common word plus the rare zephyrmodule"},
        {"id": 3, "description": "common word again"},
    ]
    ranked = _specificity_rank(items, {"common", "zephyrmodule"})
    assert ranked[0]["id"] == 2
