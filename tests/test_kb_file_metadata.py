import pytest
import yaml

from pcp import kb_file_metadata


def _make_project(tmp_path):
    """A tiny multi-file Python project with a nested package + a dir that
    must be skipped (node_modules) to prove the walk is scoped correctly."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    return 1\n")
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "util.py").write_text("def helper():\n    return 2\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("# should never be walked\n")
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    return tmp_path, pcp_dir


def test_walks_every_source_file_no_file_skipped(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    summary = kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    assert summary["total"] == 2  # app.py + pkg/util.py, node_modules excluded
    assert sorted(summary["written"]) == ["src/app.py", "src/pkg/util.py"]
    assert summary["updated"] == []
    assert summary["unchanged"] == []


def test_card_written_at_mirrored_path(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    assert card_path.exists()

    card_path_nested = pcp_dir / "kb" / "file_metadata" / "src" / "pkg" / "util.py.yaml"
    assert card_path_nested.exists()


def test_card_contents_have_required_fields(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    card = yaml.safe_load(card_path.read_text())

    assert card["source_path"] == "src/app.py"
    assert card["card_version"] == kb_file_metadata.CARD_VERSION
    assert isinstance(card["code_sha_at_verification"], str) and card["code_sha_at_verification"]
    assert "generated_at" in card
    assert card["claims"] == []


def test_rerun_with_no_changes_marks_unchanged_not_rewritten(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    first_write = card_path.read_text()

    summary = kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    assert sorted(summary["unchanged"]) == ["src/app.py", "src/pkg/util.py"]
    assert summary["written"] == []
    assert summary["updated"] == []
    assert card_path.read_text() == first_write


def test_changed_file_updates_existing_card_and_preserves_claims(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    card = yaml.safe_load(card_path.read_text())
    card["claims"] = [{"text": "returns 1", "tier": "cited", "quote": "return 1"}]
    card_path.write_text(yaml.safe_dump(card, sort_keys=False))

    # Modify the underlying source so its sha changes.
    (project_root / "src" / "app.py").write_text("def main():\n    return 99\n")

    summary = kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    assert "src/app.py" in summary["updated"]
    assert "src/app.py" not in summary["written"]

    updated_card = yaml.safe_load(card_path.read_text())
    assert updated_card["claims"] == [{"text": "returns 1", "tier": "cited", "quote": "return 1"}]
    assert "updated_at" in updated_card


def test_new_file_added_after_first_run_is_written_not_skipped(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    (project_root / "src" / "new_module.py").write_text("VALUE = 42\n")

    summary = kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    assert "src/new_module.py" in summary["written"]
    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "new_module.py.yaml"
    assert card_path.exists()


def test_card_path_for_mirrors_relative_source_path(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    source = project_root / "src" / "pkg" / "util.py"

    result = kb_file_metadata.card_path_for(pcp_dir, project_root, source)

    assert result == pcp_dir / "kb" / "file_metadata" / "src" / "pkg" / "util.py.yaml"


def test_empty_project_yields_zero_total_and_no_crash(tmp_path):
    project_root = tmp_path / "empty"
    project_root.mkdir()
    pcp_dir = project_root / ".pcp"
    pcp_dir.mkdir()

    summary = kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    assert summary == {"written": [], "updated": [], "unchanged": [], "total": 0}


# --- A003: claim content authoring -- tier:cited (literal quote) or explicit
# tier:not_grounded, a filename/function name alone never counts as evidence.


def test_author_claim_cited_with_real_quote_succeeds():
    claim = kb_file_metadata.author_claim(
        text="main() returns 1",
        tier="cited",
        quote="return 1",
    )
    assert claim == {"text": "main() returns 1", "tier": "cited", "quote": "return 1"}


def test_author_claim_not_grounded_requires_no_quote():
    claim = kb_file_metadata.author_claim(text="purpose is unclear", tier="not_grounded")
    assert claim == {"text": "purpose is unclear", "tier": "not_grounded"}


def test_author_claim_cited_without_quote_raises():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(text="does something", tier="cited")


def test_author_claim_cited_with_empty_quote_raises():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(text="does something", tier="cited", quote="   ")


def test_author_claim_bare_filename_as_quote_never_counts_as_evidence():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(
            text="this file handles cards",
            tier="cited",
            quote="kb_file_metadata.py",
        )


def test_author_claim_bare_function_name_as_quote_never_counts_as_evidence():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(
            text="this generates cards",
            tier="cited",
            quote="generate_file_metadata_cards",
        )


def test_author_claim_not_grounded_with_quote_raises():
    # A quote alongside not_grounded would blend the two evidence states.
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(
            text="purpose is unclear", tier="not_grounded", quote="return 1"
        )


def test_author_claim_invalid_tier_raises():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(text="does something", tier="probably_true")


def test_author_claim_empty_text_raises():
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.author_claim(text="   ", tier="not_grounded")


def test_validate_claims_empty_list_has_no_problems():
    assert kb_file_metadata.validate_claims([]) == []


def test_validate_claims_flags_bare_identifier_masquerading_as_quote():
    problems = kb_file_metadata.validate_claims(
        [{"text": "does the thing", "tier": "cited", "quote": "util.py"}]
    )
    assert len(problems) == 1
    assert "util.py" in problems[0]


def test_validate_claims_flags_missing_tier():
    problems = kb_file_metadata.validate_claims([{"text": "does the thing"}])
    assert len(problems) == 1


def test_validate_claims_all_valid_returns_empty():
    problems = kb_file_metadata.validate_claims(
        [
            {"text": "returns 1", "tier": "cited", "quote": "return 1"},
            {"text": "auth flow undocumented", "tier": "not_grounded"},
        ]
    )
    assert problems == []


def test_validate_claims_non_dict_entry_flagged_not_crashed():
    problems = kb_file_metadata.validate_claims(["not a mapping"])
    assert len(problems) == 1


def test_build_card_rejects_preserved_claim_that_is_not_honestly_tiered(tmp_path):
    project_root, _pcp_dir = _make_project(tmp_path)
    source_file = project_root / "src" / "app.py"
    # A claim smuggled in by hand (or corrupted) with a bare filename as its
    # "quote" -- never legitimate evidence -- must not be silently preserved
    # forward into the next generation of the card.
    existing = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "claims": [{"text": "does something", "tier": "cited", "quote": "app.py"}],
    }
    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.build_card(project_root, source_file, existing, "deadbeef")


def test_build_card_accepts_preserved_claim_that_is_honestly_tiered(tmp_path):
    project_root, _pcp_dir = _make_project(tmp_path)
    source_file = project_root / "src" / "app.py"
    existing = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "claims": [{"text": "returns 1", "tier": "cited", "quote": "return 1"}],
    }
    card = kb_file_metadata.build_card(project_root, source_file, existing, "deadbeef")
    assert card["claims"] == [{"text": "returns 1", "tier": "cited", "quote": "return 1"}]


def test_generate_file_metadata_cards_refuses_when_hand_edited_claim_is_invalid(tmp_path):
    project_root, pcp_dir = _make_project(tmp_path)
    kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)

    card_path = pcp_dir / "kb" / "file_metadata" / "src" / "app.py.yaml"
    card = yaml.safe_load(card_path.read_text())
    card["claims"] = [{"text": "returns 1", "tier": "cited", "quote": "app.py"}]
    card_path.write_text(yaml.safe_dump(card, sort_keys=False))

    # Modify the underlying source so the card is due for regeneration.
    (project_root / "src" / "app.py").write_text("def main():\n    return 99\n")

    with pytest.raises(kb_file_metadata.ClaimValidationError):
        kb_file_metadata.generate_file_metadata_cards(project_root, pcp_dir)
