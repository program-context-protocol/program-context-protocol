import yaml

from pcp import kb_index


def _make_project(tmp_path):
    """A small project with: two files sharing a basename in different dirs
    (multi-file collision), one generic-named file, one file only known via
    an existing file_metadata card (not currently on disk -- proves the
    union side), and a kb doc referencing a couple of the basenames."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "util.py").write_text("def helper():\n    return 2\n")

    (tmp_path / "src" / "widgets").mkdir()
    (tmp_path / "src" / "widgets" / "config.py").write_text("A = 1\n")
    (tmp_path / "src" / "gadgets").mkdir()
    (tmp_path / "src" / "gadgets" / "config.py").write_text("B = 2\n")
    (tmp_path / "src" / "widget_registry.py").write_text("REGISTRY = {}\n")

    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    kb_dir = pcp_dir / "kb"
    (kb_dir / "adr").mkdir(parents=True)
    (kb_dir / "adr" / "ADR-001-example.md").write_text(
        "# ADR-001\n\nSee `src/app.py` and config.py for details. Also util.py.\n"
    )
    (kb_dir / "domain").mkdir(parents=True)
    (kb_dir / "domain" / "general.md").write_text(
        "No filenames mentioned here, just prose about the domain.\n"
    )

    return tmp_path, pcp_dir


def _write_card(pcp_dir, project_root, source_rel):
    card_path = pcp_dir / "kb" / "file_metadata" / (source_rel + ".yaml")
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(
        yaml.safe_dump(
            {
                "card_version": 1,
                "source_path": source_rel,
                "code_sha_at_verification": "deadbeef",
                "generated_at": "2026-08-01T00:00:00Z",
                "claims": [],
            },
            sort_keys=False,
        )
    )


def test_union_includes_source_tree_files_with_no_card(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert "app.py" in result["index"]
    assert result["index"]["app.py"]["real_paths"] == ["src/app.py"]


def test_union_includes_carded_file_no_longer_on_disk(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    _write_card(pcp_dir, project_root, "src/legacy/gone.py")

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert "gone.py" in result["index"]
    assert result["index"]["gone.py"]["real_paths"] == ["src/legacy/gone.py"]


def test_carded_and_walked_paths_for_same_file_are_deduped(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    _write_card(pcp_dir, project_root, "src/app.py")

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["app.py"]["real_paths"] == ["src/app.py"]


def test_multi_file_collision_flagged_when_two_real_paths_share_basename(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    entry = result["index"]["config.py"]
    assert sorted(entry["real_paths"]) == ["src/gadgets/config.py", "src/widgets/config.py"]
    assert entry["multi_file_collision"] is True


def test_no_collision_for_single_path_basename(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["app.py"]["multi_file_collision"] is False


def test_generic_name_caution_flagged_for_known_generic_basename(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["config.py"]["generic_name_caution"] is True


def test_generic_name_caution_false_for_distinctive_basename(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["widget_registry.py"]["generic_name_caution"] is False


def test_kb_references_collected_from_topic_docs(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["config.py"]["kb_references"] == [".pcp/kb/adr/ADR-001-example.md"]
    assert result["index"]["util.py"]["kb_references"] == [".pcp/kb/adr/ADR-001-example.md"]
    assert result["index"]["app.py"]["kb_references"] == [".pcp/kb/adr/ADR-001-example.md"]


def test_kb_references_empty_when_basename_never_mentioned(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    _write_card(pcp_dir, project_root, "src/legacy/gone.py")

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"]["gone.py"]["kb_references"] == []


def test_kb_doc_with_no_filename_tokens_contributes_no_references(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    for entry in result["index"].values():
        assert ".pcp/kb/domain/general.md" not in entry["kb_references"]


def test_counts_and_generated_at_present(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["counts"]["total_basenames"] == len(result["index"])
    assert result["counts"]["collisions"] == 1
    assert "generated_at" in result


def test_write_kb_index_writes_yaml_file(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)

    out_path = kb_index.write_kb_index(pcp_dir, project_root)

    assert out_path == pcp_dir / "kb" / "index.yaml"
    assert out_path.exists()
    data = yaml.safe_load(out_path.read_text())
    assert "app.py" in data["index"]


def test_empty_project_yields_empty_index_no_crash(tmp_path):
    project_root = tmp_path / "empty"
    project_root.mkdir()
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()

    result = kb_index.build_basename_index(project_root, pcp_dir)

    assert result["index"] == {}
    assert result["counts"]["total_basenames"] == 0
    assert result["counts"]["collisions"] == 0
