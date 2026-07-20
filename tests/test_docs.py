import subprocess
from pathlib import Path

import yaml
from click.testing import CliRunner

from pcp.cli import cli
from pcp.commands.docs import build_module_docs, write_module_docs
from pcp import telemetry
from pcp import decision_log


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def _init_repo(tmp_path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "test@test.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    return tmp_path


def _write_module(pcp_dir, name, *, description="does something useful for widgets", criteria=None):
    mod_dir = pcp_dir / "strategy" / "modules" / name
    mod_dir.mkdir(parents=True)
    spec = {
        "version": "1.0", "module": name, "description": description,
        "objective_coverage": ["Covers the widget-handling part of the objective"],
        "dependencies": ["core"], "constraints": ["must be fast"],
    }
    (mod_dir / "spec.yaml").write_text(yaml.dump(spec))
    acc = {"version": "1.0", "module": name, "criteria": criteria or [
        {"id": "A001", "description": "core impl", "check": "manual", "status": "complete"},
        {"id": "A002", "description": "second thing", "check": "manual", "status": "pending"},
    ]}
    (mod_dir / "acceptance.yaml").write_text(yaml.dump(acc))
    return mod_dir


def _commit_all(repo):
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "scaffold"], repo)


# ── build_module_docs, pure aggregation ──

def test_vision_pulls_from_spec(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    data = build_module_docs(pcp_dir, mod_dir)
    assert data["spec"]["description"] == "does something useful for widgets"
    assert data["criteria"][0]["id"] == "A001"


def test_brd_keyword_match_finds_relevant_items(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", description="handles widget rendering")
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": [
        {"id": "BRD-001", "status": "active", "description": "widgets must render within 200ms"},
        {"id": "BRD-002", "status": "active", "description": "completely unrelated payments requirement"},
    ]}))
    _commit_all(repo)

    data = build_module_docs(pcp_dir, mod_dir)
    matched_ids = {i["id"] for i in data["matched_brd"]}
    assert "BRD-001" in matched_ids
    assert "BRD-002" not in matched_ids


def test_changelog_merges_build_and_spec_history_chronologically(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)  # spec.yaml commit #1

    # spec changes again later
    spec_path = mod_dir / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text())
    spec["constraints"].append("also must be cheap")
    spec_path.write_text(yaml.dump(spec))
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "widen widgets scope"], repo)

    telemetry.record(
        pcp_dir, cycle="build", module="widgets", criterion_id="A001",
        files=["src/widgets.py"], lines_added=10, lines_removed=0,
        timestamp="2026-01-01T00:00:00Z",
    )

    data = build_module_docs(pcp_dir, mod_dir)
    kinds = [e["kind"] for e in data["timeline"]]
    assert "build" in kinds
    assert "spec_change" in kinds
    # chronological, oldest first
    timestamps = [e["timestamp"] for e in data["timeline"]]
    assert timestamps == sorted(timestamps)


def test_changelog_includes_module_tagged_decisions(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    decision_log.record(
        pcp_dir, source="build:widgets:A001", module="widgets", category="architecture",
        summary="chose SQLite for widget storage",
    )
    decision_log.record(pcp_dir, source="build:other:A001", module="other", category="architecture", summary="unrelated")

    data = build_module_docs(pcp_dir, mod_dir)
    decisions = [e for e in data["timeline"] if e["kind"] == "decision"]
    assert len(decisions) == 1
    assert "SQLite" in decisions[0]["summary"]


def test_changelog_includes_module_attributed_bypasses(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    (pcp_dir / "bypass_log.yaml").write_text(yaml.dump({"bypasses": [
        {"timestamp": "2026-01-01T00:00:00Z", "reason": "known false positive",
         "files": ["src/widgets.py"], "modules": ["widgets"]},
        {"timestamp": "2026-01-02T00:00:00Z", "reason": "unrelated module bypass",
         "files": ["src/other.py"], "modules": ["other"]},
        {"timestamp": "2026-01-03T00:00:00Z", "reason": "legacy entry, pre-attribution",
         "files": ["src/legacy.py"]},
    ]}))

    data = build_module_docs(pcp_dir, mod_dir)
    bypass_entries = [e for e in data["timeline"] if e["kind"] == "bypass"]
    assert len(bypass_entries) == 1
    assert bypass_entries[0]["reason"] == "known false positive"
    assert data["bypass_count"] == 1


def test_drift_score_flags_spec_change_between_two_builds(tmp_path):
    from datetime import datetime, timedelta, timezone

    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    # Deliberately NOT committed yet -- spec.yaml/acceptance.yaml have no git
    # history at all until the one commit below, so that commit is the ONLY
    # spec/acceptance-change event on the timeline (a clean, deterministic
    # count instead of also picking up an initial-scaffold commit).

    before = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    telemetry.record(
        pcp_dir, cycle="build", module="widgets", criterion_id="A001",
        files=["src/widgets.py"], lines_added=10, lines_removed=0,
        timestamp=before,
    )

    # spec + acceptance change mid-build -- git commit timestamps are real
    # system time, bracketed here by build timestamps 5 min before/after.
    spec_path = mod_dir / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text())
    spec["constraints"].append("also must be cheap")
    spec_path.write_text(yaml.dump(spec))
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "widen widgets scope"], repo)

    after = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    telemetry.record(
        pcp_dir, cycle="build", module="widgets", criterion_id="A002",
        files=["src/widgets2.py"], lines_added=5, lines_removed=0,
        timestamp=after,
    )

    data = build_module_docs(pcp_dir, mod_dir)
    drift = data["drift"]
    # One commit touching both spec.yaml and acceptance.yaml == 2 in-flight events.
    assert len(drift["in_flight_changes"]) == 2
    assert drift["score"] > 0


def test_drift_score_zero_with_no_activity(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    data = build_module_docs(pcp_dir, mod_dir)
    assert data["drift"] == {"score": 0.0, "in_flight_changes": [], "bypass_count": 0, "retry_count": 0}


def test_drift_score_counts_retries_and_bypasses(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    # Two build attempts for the same criterion == 1 retry.
    telemetry.record(pcp_dir, cycle="build", cycle_number=1, module="widgets", criterion_id="A001",
                      files=[], timestamp="2026-01-01T00:00:00Z")
    telemetry.record(pcp_dir, cycle="build", cycle_number=2, module="widgets", criterion_id="A001",
                      files=[], timestamp="2026-01-01T00:05:00Z")

    (pcp_dir / "bypass_log.yaml").write_text(yaml.dump({"bypasses": [
        {"timestamp": "2026-01-01T00:02:00Z", "reason": "known false positive",
         "files": ["src/widgets.py"], "modules": ["widgets"]},
    ]}))

    data = build_module_docs(pcp_dir, mod_dir)
    drift = data["drift"]
    assert drift["retry_count"] == 1
    assert drift["bypass_count"] == 1
    assert drift["score"] == round(0.2 * 1 + 0.1 * 1, 2)


def test_no_activity_yields_empty_timeline(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    # deliberately not committed -- no git history yet, no telemetry, no decisions
    data = build_module_docs(pcp_dir, mod_dir)
    assert data["timeline"] == []


# ── write_module_docs, rendered files ──

def test_write_module_docs_creates_all_six_files(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    assert (out_dir / "vision.md").exists()
    assert (out_dir / "brd.md").exists()
    assert (out_dir / "built.md").exists()
    assert (out_dir / "changelog.md").exists()
    assert (out_dir / "ui_ux.md").exists()
    assert (out_dir / "diff.md").exists()

    vision = (out_dir / "vision.md").read_text()
    assert "does something useful for widgets" in vision
    built = (out_dir / "built.md").read_text()
    assert "A001" in built
    assert "1/2 criteria complete" in built


# ── ui_ux.md ──

def test_ui_ux_empty_for_non_ui_module(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", criteria=[
        {"id": "A001", "description": "API returns correct percentage", "check": "manual", "status": "complete"},
    ])
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    ui_ux = (out_dir / "ui_ux.md").read_text()
    assert "No UI-facing criteria" in ui_ux


def test_ui_ux_flags_missing_design_justification(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", criteria=[
        {"id": "A001", "description": "Dashboard renders coverage", "check": "manual", "status": "complete"},
    ])
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    ui_ux = (out_dir / "ui_ux.md").read_text()
    assert "no `design_justification` declared" in ui_ux
    assert "A001" in ui_ux


def test_ui_ux_rolls_up_organisms_and_archetypes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", criteria=[
        {"id": "A001", "description": "Dashboard renders coverage", "check": "manual", "status": "complete",
         "screen_archetypes": ["dashboard"], "ui_organisms": ["kpi-tile", "chart-panel"],
         "nav_depth": 2,
         "design_justification": {"checklist_passed": ["both-themes"], "jtbd_framing": "when a user checks status, this shows it",
                                   "customizable": True}},
    ])
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    ui_ux = (out_dir / "ui_ux.md").read_text()
    assert "dashboard" in ui_ux
    assert "kpi-tile, chart-panel" in ui_ux
    assert "when a user checks status" in ui_ux


# ── diff.md ──

def test_diff_lists_pending_criteria(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")  # A001 complete, A002 pending
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    diff = (out_dir / "diff.md").read_text()
    assert "A002" in diff
    assert "second thing" in diff
    assert "A001" not in diff.split("Pending Acceptance Criteria")[1]


def test_diff_flags_unaddressed_active_brd_item(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", description="handles widget rendering", criteria=[
        {"id": "A001", "description": "unrelated backend plumbing", "check": "manual", "status": "complete"},
    ])
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": [
        {"id": "BRD-001", "status": "active", "description": "widgets must render within 200ms"},
    ]}))
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    diff = (out_dir / "diff.md").read_text()
    assert "BRD-001" in diff
    assert "no completed criterion keyword-matches" in diff


def test_diff_finds_likely_match_via_keyword_overlap(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets", description="handles widget rendering", criteria=[
        {"id": "A001", "description": "widgets render within performance budget", "check": "manual", "status": "complete"},
    ])
    (pcp_dir / "brd_items.yaml").write_text(yaml.dump({"items": [
        {"id": "BRD-001", "status": "active", "description": "widgets must render within 200ms"},
    ]}))
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    diff = (out_dir / "diff.md").read_text()
    assert "likely addressed by: A001" in diff


def test_changelog_drift_signal_wording_present(tmp_path):
    """The doc must actually explain what a spec-change-between-builds means,
    not just list events -- that's the difference between a changelog and a
    drift ledger."""
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    _commit_all(repo)

    out_dir = write_module_docs(pcp_dir, mod_dir)
    changelog = (out_dir / "changelog.md").read_text()
    assert "drift signal" in changelog.lower()
    assert "bypass_log.yaml" in changelog  # honest gap disclosure, not silently omitted


# ── CLI ──

def test_docs_cli_generates_for_all_modules(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "widgets")
    _write_module(pcp_dir, "gadgets", description="handles gadget assembly")
    _commit_all(repo)

    runner = CliRunner()
    result = runner.invoke(cli, ["docs", "--path", str(repo)])
    assert result.exit_code == 0, result.output
    assert (pcp_dir / "strategy" / "modules" / "widgets" / "docs" / "vision.md").exists()
    assert (pcp_dir / "strategy" / "modules" / "gadgets" / "docs" / "vision.md").exists()


def test_docs_cli_single_module_flag(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    _write_module(pcp_dir, "widgets")
    _write_module(pcp_dir, "gadgets")
    _commit_all(repo)

    runner = CliRunner()
    result = runner.invoke(cli, ["docs", "--module", "widgets", "--path", str(repo)])
    assert result.exit_code == 0, result.output
    assert (pcp_dir / "strategy" / "modules" / "widgets" / "docs" / "vision.md").exists()
    assert not (pcp_dir / "strategy" / "modules" / "gadgets" / "docs").exists()


def test_docs_cli_no_modules_directory(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["docs", "--path", str(repo)])
    assert result.exit_code == 0
    assert "No module" in result.output
