"""Work that landed but is still marked pending.

Three occurrences on Project O in a week via two different paths: the
wave-gate reopen ($30.04, four core-data-model criteria merged then reverted to
pending) and a run that simply stopped ($31.65, query-eval-harness, A001/A008
merged into main with source and tests present, all 18 criteria reading pending).

The status being wrong in this direction is the worst kind: the next build redoes
finished work, and current_state.md / the dashboard / validate-strategy all
inherit the lie.
"""
import subprocess

import yaml

from pcp.orphaned_work import find_orphaned_work, format_findings


def _project(tmp_path, criteria, commits=()):
    root = tmp_path / "p"
    mod = root / ".pcp" / "strategy" / "modules" / "billing"
    mod.mkdir(parents=True)
    (mod / "acceptance.yaml").write_text(yaml.dump({
        "version": "2.0", "module": "billing", "criteria": criteria,
    }))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    for msg in commits:
        (root / "f.txt").write_text((root / "f.txt").read_text() + "y")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", msg], cwd=root, check=True)
    return root


def test_merge_commit_for_a_pending_criterion_is_reported(tmp_path):
    root = _project(tmp_path, [{"id": "A001", "description": "d", "check": "manual",
                                "status": "pending"}],
                    commits=["Merge feat/billing-A001"])
    found = find_orphaned_work(root / ".pcp", root)
    assert len(found) == 1
    assert found[0]["criterion_id"] == "A001"
    assert found[0]["evidence"] == "Merge feat/billing-A001"


def test_auto_commit_convention_is_also_evidence(tmp_path):
    root = _project(tmp_path, [{"id": "A002", "description": "d", "check": "manual",
                                "status": "pending"}],
                    commits=["billing/A002: add the charge endpoint"])
    found = find_orphaned_work(root / ".pcp", root)
    assert len(found) == 1 and found[0]["criterion_id"] == "A002"


def test_a_completed_criterion_is_never_reported(tmp_path):
    root = _project(tmp_path, [{"id": "A001", "description": "d", "check": "manual",
                                "status": "complete"}],
                    commits=["Merge feat/billing-A001"])
    assert find_orphaned_work(root / ".pcp", root) == []


def test_pending_with_no_landed_commit_is_not_reported(tmp_path):
    """Genuinely unbuilt work must stay quiet, or the signal is worthless."""
    root = _project(tmp_path, [{"id": "A001", "description": "d", "check": "manual",
                                "status": "pending"}])
    assert find_orphaned_work(root / ".pcp", root) == []


def test_a_merged_branch_that_never_carried_a_commit_is_not_evidence(tmp_path):
    """The first version used `git branch --merged` and was wrong on 6 of 18
    findings: pcp build resets reused worktree branches to the current base, so a
    branch with no commits is an ancestor of HEAD by construction."""
    root = _project(tmp_path, [{"id": "MOD_A001", "description": "d", "check": "manual",
                                "status": "pending"}])
    subprocess.run(["git", "branch", "feat/billing-MOD_A001"], cwd=root, check=True)
    assert find_orphaned_work(root / ".pcp", root) == []


def test_a_similar_criterion_id_does_not_cross_match(tmp_path):
    """A001 must not be credited by a commit for A0011."""
    root = _project(tmp_path, [{"id": "A001", "description": "d", "check": "manual",
                                "status": "pending"}],
                    commits=["Merge feat/billing-A0011"])
    assert find_orphaned_work(root / ".pcp", root) == []


def test_non_git_directory_is_silent(tmp_path):
    d = tmp_path / "nogit" / ".pcp" / "strategy" / "modules" / "m"
    d.mkdir(parents=True)
    (d / "acceptance.yaml").write_text(yaml.dump({"version": "2.0", "module": "m",
        "criteria": [{"id": "A1", "description": "d", "check": "manual", "status": "pending"}]}))
    assert find_orphaned_work(tmp_path / "nogit" / ".pcp", tmp_path / "nogit") == []


def test_format_is_empty_when_nothing_found():
    assert format_findings([]) == []


def test_format_names_the_evidence_and_refuses_to_auto_fix():
    """Fixed 2026-08-08 (win2mac dogfood): this used to recommend `pcp pm` for
    a pure status flip, which regenerates the whole spec from an LLM and can
    silently drop real content. Must point at `pcp verify` instead -- the
    surgical, status-only, deterministic write path."""
    lines = format_findings([{"module": "billing", "criterion_id": "A001",
                              "branch": "feat/billing-A001", "status": "pending",
                              "evidence": "Merge feat/billing-A001", "description": "d"}])
    body = "\n".join(lines)
    assert "Merge feat/billing-A001" in body
    assert "pcp verify billing A001" in body
    assert "Do NOT use `pcp pm`" in body
    assert "human-approved" not in body  # old (buggy) message text, must be gone
