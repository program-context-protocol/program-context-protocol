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


def test_no_activity_yields_empty_timeline(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    pcp_dir = repo / ".pcp"
    pcp_dir.mkdir()
    mod_dir = _write_module(pcp_dir, "widgets")
    # deliberately not committed -- no git history yet, no telemetry, no decisions
    data = build_module_docs(pcp_dir, mod_dir)
    assert data["timeline"] == []


# ── write_module_docs, rendered files ──

def test_write_module_docs_creates_all_four_files(tmp_path):
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

    vision = (out_dir / "vision.md").read_text()
    assert "does something useful for widgets" in vision
    built = (out_dir / "built.md").read_text()
    assert "A001" in built
    assert "1/2 criteria complete" in built


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
