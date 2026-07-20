from pathlib import Path

from pcp import librarian


def test_find_related_definitions_matches_on_keyword_overlap(tmp_path):
    (tmp_path / "invoice_render.py").write_text(
        "def render_invoice_summary(data):\n    pass\n\n\ndef unrelated_helper():\n    pass\n"
    )
    criterion = {"id": "BF_001", "description": "render an invoice summary for the customer"}
    hits = librarian.find_related_definitions(tmp_path, criterion)
    assert any("render_invoice_summary" in h for h in hits)
    assert not any("unrelated_helper" in h for h in hits)


def test_find_related_definitions_ranks_higher_overlap_first(tmp_path):
    (tmp_path / "a.py").write_text("def invoice_summary_export():\n    pass\n")
    (tmp_path / "b.py").write_text("def invoice_helper():\n    pass\n")
    criterion = {"id": "BF_001", "description": "export invoice summary data"}
    hits = librarian.find_related_definitions(tmp_path, criterion)
    assert hits[0].endswith("invoice_summary_export")


def test_find_related_definitions_returns_empty_with_no_keywords(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    criterion = {"id": "1", "description": "a a a"}
    assert librarian.find_related_definitions(tmp_path, criterion) == []


def test_find_related_definitions_skips_skip_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.py").write_text("def invoice_summary_export():\n    pass\n")
    criterion = {"id": "BF_001", "description": "invoice summary export"}
    assert librarian.find_related_definitions(tmp_path, criterion) == []


def test_find_related_definitions_respects_max_results(tmp_path):
    for i in range(10):
        (tmp_path / f"m{i}.py").write_text(f"def invoice_summary_export_{i}():\n    pass\n")
    criterion = {"id": "BF_001", "description": "invoice summary export"}
    hits = librarian.find_related_definitions(tmp_path, criterion, max_results=3)
    assert len(hits) == 3


def test_format_for_prompt_bounds_by_max_chars(tmp_path):
    (tmp_path / "a.py").write_text("def invoice_summary_export():\n    pass\n")
    criterion = {"id": "BF_001", "description": "invoice summary export"}
    lines = librarian.format_for_prompt(tmp_path, criterion, max_chars=5)
    assert lines == []


def test_format_for_prompt_returns_dash_prefixed_lines(tmp_path):
    (tmp_path / "a.py").write_text("def invoice_summary_export():\n    pass\n")
    criterion = {"id": "BF_001", "description": "invoice summary export"}
    lines = librarian.format_for_prompt(tmp_path, criterion)
    assert lines and all(l.startswith("- ") for l in lines)


def test_find_related_definitions_handles_nonexistent_root(tmp_path):
    criterion = {"id": "BF_001", "description": "invoice summary export"}
    assert librarian.find_related_definitions(tmp_path / "does-not-exist", criterion) == []
