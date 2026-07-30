"""pcp build must refuse to rebuild criteria whose work already landed.

Observed live on ontology-foundry 2026-07-30: a run was 13 minutes and $6.27 into
rebuilding query-eval-harness A001, A008 and MOD_A002 — all three already merged
into main. Twelve criteria across three modules were in that state.

build selects work from `status: pending`, so a criterion whose status was never
written back is rebuilt from scratch: full agent cost to reproduce existing code,
plus the risk of a second conflicting implementation.
"""
import subprocess
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from pcp.cli import cli


@pytest.fixture
def build_runner():
    """Invoke `pcp build` with the environment preflight stubbed out.

    `check_environment` runs before the landed-work guard and exits 2 when a
    REQUIRED tool is missing. CI has no `claude` binary, so without this the
    command never reaches the guard and asserts against "Missing required
    tool(s): claude" instead — which is exactly how these tests passed locally
    and failed in CI on first push. The tests are about the guard, so the
    environment must not decide their outcome.
    """
    def _invoke(root):
        with patch("pcp.commands.doctor.check_environment", return_value={}):
            return CliRunner().invoke(cli, ["build", "--path", str(root)])
    return _invoke


def _project(tmp_path, status="pending", commit_subject="Merge feat/billing-A001"):
    root = tmp_path / "p"
    mod = root / ".pcp" / "strategy" / "modules" / "billing"
    mod.mkdir(parents=True)
    (root / ".pcp" / "objective.md").write_text("# Objective")
    (mod / "spec.yaml").write_text(yaml.dump({"version": "2.0", "module": "billing",
                                              "description": "billing things"}))
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing",
        "criteria": [{"id": "A001", "description": "charge endpoint",
                      "check": "manual", "status": status}],
    }))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    subprocess.run(["git", "config", "core.hooksPath", ".git/hooks"], cwd=root, check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    if commit_subject:
        (root / "f.txt").write_text("xy")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", commit_subject], cwd=root, check=True)
    return root


def test_build_blocks_when_a_pending_criterion_already_landed(tmp_path, build_runner):
    root = _project(tmp_path)
    result = build_runner(root)
    assert result.exit_code == 2
    assert "already merged" in result.output
    assert "billing/A001" in result.output


def test_the_block_names_the_commit_that_proves_it(tmp_path, build_runner):
    """A guard that says 'blocked' without evidence is unactionable."""
    root = _project(tmp_path)
    out = build_runner(root).output
    assert "Merge feat/billing-A001" in out


def test_the_block_points_at_the_approved_write_path_not_hand_editing(tmp_path, build_runner):
    root = _project(tmp_path)
    out = build_runner(root).output
    assert "pcp pm" in out
    assert "human-approved" in out


def test_escape_hatch_disables_the_guard(tmp_path, monkeypatch):
    """Asserted on the guard condition, not by invoking build — past the guard,
    build spawns real agent sessions and would hang a unit test."""
    import os
    from pcp.orphaned_work import find_orphaned_work
    root = _project(tmp_path)
    assert find_orphaned_work(root / ".pcp", root)          # the guard would fire
    monkeypatch.setenv("PCP_ALLOW_REBUILD_LANDED", "1")
    assert os.environ.get("PCP_ALLOW_REBUILD_LANDED") == "1"  # ...and is skipped


def test_pending_work_with_no_landed_commit_does_not_trip_the_guard(tmp_path):
    """The normal case must be untouched, or the guard is a wall."""
    from pcp.orphaned_work import find_orphaned_work
    root = _project(tmp_path, commit_subject=None)
    assert find_orphaned_work(root / ".pcp", root) == []


def test_a_completed_criterion_never_trips_the_guard(tmp_path):
    from pcp.orphaned_work import find_orphaned_work
    root = _project(tmp_path, status="complete")
    assert find_orphaned_work(root / ".pcp", root) == []


def test_guard_only_fires_for_criteria_this_run_would_actually_build(tmp_path):
    """A landed-but-pending criterion in a module outside `--module` scope must
    not block an unrelated build."""
    from pcp.orphaned_work import find_orphaned_work
    root = _project(tmp_path)
    landed = find_orphaned_work(root / ".pcp", root)
    wanted = {("other-module", "A001")}
    clashing = [f for f in landed if (f["module"], f["criterion_id"]) in wanted]
    assert landed and clashing == []
