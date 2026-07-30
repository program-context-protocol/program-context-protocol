"""pcp build-plan — deterministic execution plan, no agents spawned.

Root of the 2026-07-30 redesign: pcp build's own Python execution engine
(worktree-per-criterion, merge/retry) was one of two independent orchestration
implementations in this codebase -- the /pcp skill's SKILL.md already
documented a second one using the Workflow tool's pipeline()/parallel(). This
splits planning (stays Python, deterministic) from execution (moves to the
harness). Measured cause: 5/5 completed query-eval-harness criteria collided
on __init__.py in one run because every criterion has to add a method to a
shared facade class and remove an entry from a shared _PENDING dict -- neither
declared as any criterion's `target`, so no scheduler could see the collision
coming from declared targets alone.
"""
import subprocess

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.build_plan import build_plan, _module_shared_surface


def _project(tmp_path):
    root = tmp_path / "p"
    pcp_dir = root / ".pcp"
    (pcp_dir / "strategy" / "modules" / "billing").mkdir(parents=True)
    (pcp_dir / "strategy" / "modules" / "billing" / "spec.yaml").write_text(
        yaml.dump({"version": "2.0", "module": "billing", "description": "d", "dependencies": []})
    )
    (pcp_dir / "strategy" / "modules" / "billing" / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [
            {"id": "MOD_A002", "description": "register", "check": "ast_pattern",
             "target": "src/main", "status": "pending"},
            {"id": "MOD_A004", "description": "interface", "check": "file_exists",
             "target": "src/interfaces/IBilling.ts", "status": "pending"},
            {"id": "A001", "description": "charge endpoint", "check": "manual",
             "target": "src/modules/billing/charge.py", "status": "pending"},
            {"id": "A002", "description": "refund endpoint", "check": "manual",
             "target": "src/modules/billing/refund.py", "status": "pending",
             "depends_on": ["A001"]},
        ],
    }))
    return pcp_dir


def test_shared_surface_always_includes_the_module_facade(tmp_path):
    pcp_dir = _project(tmp_path)
    surface = _module_shared_surface(pcp_dir, "billing")
    assert "src/modules/billing/__init__.py" in surface


def test_shared_surface_includes_declared_MOD_targets(tmp_path):
    pcp_dir = _project(tmp_path)
    surface = _module_shared_surface(pcp_dir, "billing")
    assert "src/main" in surface                      # MOD_A002's target
    assert "src/interfaces/IBilling.ts" in surface     # MOD_A004's target


def test_shared_surface_ignores_non_MOD_criteria_targets(tmp_path):
    """A001's own file (charge.py) is genuinely disjoint -- it must not be
    treated as shared just because it has a target."""
    pcp_dir = _project(tmp_path)
    surface = _module_shared_surface(pcp_dir, "billing")
    assert "src/modules/billing/charge.py" not in surface


def test_every_criterion_is_marked_as_touching_shared_surface(tmp_path):
    """The whole point: even A001, whose OWN target is its own new file, is
    still marked -- because registering it touches __init__.py regardless of
    what it declares. This is what a target-only scheduler cannot see."""
    pcp_dir = _project(tmp_path)
    plan = build_plan(pcp_dir)
    mod = plan["modules"][0]
    all_criteria = [c for wave in mod["criterion_waves"] for c in wave]
    assert all(c["touches_shared_surface"] for c in all_criteria)


def test_dependency_order_is_preserved_as_separate_waves(tmp_path):
    """A002 depends_on A001 -- must land in a LATER wave, not the same one."""
    pcp_dir = _project(tmp_path)
    plan = build_plan(pcp_dir)
    mod = plan["modules"][0]
    wave_of = {c["id"]: i for i, wave in enumerate(mod["criterion_waves"]) for c in wave}
    assert wave_of["A002"] > wave_of["A001"]


def test_independent_criteria_share_a_wave(tmp_path):
    """MOD_A002, MOD_A004, A001 have no depends_on between them -- all wave 0."""
    pcp_dir = _project(tmp_path)
    plan = build_plan(pcp_dir)
    mod = plan["modules"][0]
    wave0_ids = {c["id"] for c in mod["criterion_waves"][0]}
    assert {"MOD_A002", "MOD_A004", "A001"} <= wave0_ids


def test_no_agents_spawned_no_files_written(tmp_path):
    """The whole premise: this command must be side-effect-free on the project."""
    pcp_dir = _project(tmp_path)
    before = sorted(p.relative_to(pcp_dir) for p in pcp_dir.rglob("*") if p.is_file())
    build_plan(pcp_dir)
    after = sorted(p.relative_to(pcp_dir) for p in pcp_dir.rglob("*") if p.is_file())
    assert before == after


def test_total_criteria_count_is_accurate(tmp_path):
    pcp_dir = _project(tmp_path)
    plan = build_plan(pcp_dir)
    assert plan["total_criteria"] == 4


def test_cli_emits_valid_json(tmp_path):
    import json
    pcp_dir = _project(tmp_path)
    result = CliRunner().invoke(cli, ["build-plan", "--path", str(pcp_dir.parent)])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["total_criteria"] == 4


def test_module_filter_narrows_the_plan(tmp_path):
    pcp_dir = _project(tmp_path)
    (pcp_dir / "strategy" / "modules" / "other").mkdir(parents=True)
    (pcp_dir / "strategy" / "modules" / "other" / "spec.yaml").write_text(
        yaml.dump({"version": "2.0", "module": "other", "description": "d"}))
    (pcp_dir / "strategy" / "modules" / "other" / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "other",
        "criteria": [{"id": "A001", "description": "d", "check": "manual", "status": "pending"}],
    }))
    plan = build_plan(pcp_dir, module_name="billing")
    assert [m["name"] for m in plan["modules"]] == ["billing"]


def test_empty_project_returns_empty_plan(tmp_path):
    root = tmp_path / "p"
    pcp_dir = root / ".pcp"
    (pcp_dir / "strategy" / "modules").mkdir(parents=True)
    plan = build_plan(pcp_dir)
    assert plan == {"modules": [], "total_criteria": 0}
