"""Tests for kb_bootstrap.py (A017) -- the new-project progressive-build
hook that lets kb module components 1-4 populate as `pcp build` creates
files, with topic finalization (component 2) deliberately reserved for
kickoff time instead of running per-file during build."""

import sys

import yaml

from pcp import kb_bootstrap


def _make_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return tmp_path, pcp_dir


# --- component 1 (file-metadata cards) progressively populates ----------

def test_progressive_bootstrap_runs_component_1_file_metadata(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    assert result["component_1_file_metadata"]["ran"] is True
    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    assert card_path.exists()
    card = yaml.safe_load(card_path.read_text())
    assert card["source_path"] == "src/app.py"


def test_progressive_bootstrap_picks_up_new_files_created_between_runs(tmp_path):
    """Mirrors the real `pcp build` shape: files appear one at a time across
    criteria, not all at once. Running the hook after each new file must
    add a card for the new file without disturbing the earlier one."""
    project_root, pcp_dir = _make_project(tmp_path)

    kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)
    first_card = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    first_card_contents = first_card.read_text()

    (project_root / "src" / "second.py").write_text("VALUE = 2\n")
    kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    second_card = pcp_dir / "kb" / "file_metadata" / "src" / "second.py.yaml"
    assert second_card.exists()
    # First card untouched -- unchanged file, not rewritten.
    assert first_card.read_text() == first_card_contents


# --- components 3/4 don't exist yet -- forward-compatible, not hardcoded ---

def test_progressive_bootstrap_reports_unbuilt_components_without_crashing(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    assert result["component_3_catalog_index"]["ran"] is False
    assert result["component_4_gap_ingestion"]["ran"] is False


def test_progressive_bootstrap_picks_up_a_component_once_its_module_exists(tmp_path, monkeypatch):
    """Proves the dispatch is real discovery, not a hardcoded single-component
    call -- once a later criterion ships pcp.kb_catalog, this hook picks it
    up with zero edits to kb_bootstrap.py itself."""
    import types

    project_root, pcp_dir = _make_project(tmp_path)

    calls = []

    stub = types.ModuleType("pcp.kb_catalog")

    def build_catalog_and_index(proj_root, pdir):
        calls.append((proj_root, pdir))
        return {"built": True}

    stub.build_catalog_and_index = build_catalog_and_index
    monkeypatch.setitem(sys.modules, "pcp.kb_catalog", stub)

    result = kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    assert calls == [(project_root, pcp_dir)]
    assert result["component_3_catalog_index"] == {"ran": True, "summary": {"built": True}}


def test_progressive_bootstrap_never_raises_when_a_component_errors(tmp_path, monkeypatch):
    project_root, pcp_dir = _make_project(tmp_path)

    def _boom(_project_root, _pcp_dir):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(kb_bootstrap, "_load_component", lambda module_name, func_name: (
        _boom if module_name == "pcp.kb_file_metadata" else None
    ))

    result = kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    assert result["component_1_file_metadata"]["ran"] is False
    assert "kaboom" in result["component_1_file_metadata"]["reason"]


# --- component 2 (topic finalization) is kickoff-time only, not per-build ---

def test_topic_finalization_writes_topics_yaml(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    (pcp_dir / "objective.md").write_text("# Program Objective\n\nBuild things.\n")

    result = kb_bootstrap.run_topic_finalization(project_root, pcp_dir)

    assert result["ran"] is True
    out_path = pcp_dir / "kb" / "topics.yaml"
    assert out_path.exists()
    data = yaml.safe_load(out_path.read_text())
    assert any(t["label"] == "Program Objective" for t in data["topics"])


def test_topic_finalization_never_raises_on_error(tmp_path, monkeypatch):
    project_root, pcp_dir = _make_project(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr("pcp.commands.kb_topics.write_kb_topics", _boom)

    result = kb_bootstrap.run_topic_finalization(project_root, pcp_dir)

    assert result["ran"] is False
    assert "derivation failed" in result["reason"]


def test_run_progressive_bootstrap_does_not_call_topic_finalization(tmp_path, monkeypatch):
    """Component 2 is explicitly NOT part of the per-file progressive hook --
    it is aggregated whole-project derivation, finalized once at kickoff."""
    project_root, pcp_dir = _make_project(tmp_path)

    called = []
    monkeypatch.setattr(
        "pcp.commands.kb_topics.write_kb_topics",
        lambda *a, **k: called.append(True),
    )

    kb_bootstrap.run_progressive_bootstrap(project_root, pcp_dir)

    assert called == []


# --- run_eager_import_sweep (A018) -- existing-project one-shot full sweep ---

def _make_import_project(tmp_path):
    """A project shaped like `pcp import` just scaffolded it: objective.md +
    a module spec already on disk (import writes these before the sweep
    runs), plus one real source file for component 1 to card."""
    project_root, pcp_dir = _make_project(tmp_path)
    (pcp_dir / "objective.md").write_text("# Program Objective\n\nBuild things.\n")
    (pcp_dir / "target_state.md").write_text("# Target State\n\nDone means shipped.\n")
    mod_dir = pcp_dir / "strategy" / "modules" / "widgets"
    mod_dir.mkdir(parents=True)
    (mod_dir / "spec.yaml").write_text(
        "module: widgets\ndescription: x\ndependencies: []\nconstraints: []\n"
    )
    return project_root, pcp_dir


def test_eager_import_sweep_runs_component_1_file_metadata(tmp_path):
    project_root, pcp_dir = _make_import_project(tmp_path)

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    assert result["component_1_file_metadata"]["ran"] is True
    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    assert card_path.exists()


def test_eager_import_sweep_runs_component_2_topics(tmp_path):
    project_root, pcp_dir = _make_import_project(tmp_path)

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    assert result["component_2_topics"]["ran"] is True
    topics_path = pcp_dir / "kb" / "topics.yaml"
    assert topics_path.exists()
    data = yaml.safe_load(topics_path.read_text())
    assert any(t["label"] == "Program Objective" for t in data["topics"])


def test_eager_import_sweep_runs_component_3_catalog_and_index(tmp_path):
    project_root, pcp_dir = _make_import_project(tmp_path)
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True)
    (domain_dir / "notes.md").write_text("# Heading One\n\nsome prose\n")

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    assert result["component_3_catalog_index"]["ran"] is True
    assert (pcp_dir / "kb" / "catalog" / "domain.yaml").exists()
    assert (pcp_dir / "kb" / "index.yaml").exists()


def test_eager_import_sweep_runs_component_4_gap_detection_after_topics_exist(tmp_path):
    """Component 4 (gap detection) reads topics.yaml -- proves the sweep
    runs component 2 before component 4, not in some other order, since
    topics.yaml doesn't exist until component 2 writes it."""
    project_root, pcp_dir = _make_import_project(tmp_path)

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    assert result["component_4_gap_ingestion"]["ran"] is True
    summary = result["component_4_gap_ingestion"]["summary"]
    # "Program Objective" topic has zero kb/domain+kb/adr content -> a gap.
    assert summary["gap_count"] >= 1


def test_eager_import_sweep_runs_once_synchronously_not_per_file(tmp_path):
    """Unlike run_progressive_bootstrap, this is a single explicit call the
    caller (`pcp import`) makes exactly once -- no hidden re-entrancy or
    background scheduling here."""
    project_root, pcp_dir = _make_import_project(tmp_path)

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    for key in (
        "component_1_file_metadata",
        "component_2_topics",
        "component_3_catalog_index",
        "component_4_gap_ingestion",
    ):
        assert key in result


def test_eager_import_sweep_never_raises_when_a_component_errors(tmp_path, monkeypatch):
    project_root, pcp_dir = _make_import_project(tmp_path)

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("pcp.kb_file_metadata.generate_file_metadata_cards", _boom)

    result = kb_bootstrap.run_eager_import_sweep(project_root, pcp_dir)

    assert result["component_1_file_metadata"]["ran"] is False
    assert "kaboom" in result["component_1_file_metadata"]["reason"]
    # Other components still ran -- one failure doesn't abort the sweep.
    assert result["component_2_topics"]["ran"] is True
