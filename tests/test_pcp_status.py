from pcp.pcp_status import write_pcp_md


def _write(pcp_dir, timestamp="2026-07-08T00:00:00Z"):
    return write_pcp_md(pcp_dir, modules_results=[], timestamp=timestamp, total=0, complete=0)


def test_objective_extracted_when_heading_directly_precedes_body(tmp_path):
    """Real bug: a heading immediately followed by its body text on the very
    next line (no blank line -- completely normal markdown) used to make the
    whole heading+body block get rejected as 'starts with #', silently
    discarding real objective text and falsely reporting no objective.md
    even though the file existed."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text(
        "# Program Objective\n\n"
        "## Why This Exists\n"
        "Because agents need a shared memory of trading failures.\n\n"
        "## Out of Scope\n"
        "Human curation."
    )
    out = _write(pcp_dir)
    md = out.read_text()
    assert "No objective.md found" not in md
    assert "Because agents need a shared memory" in md


def test_objective_extracted_with_blank_line_between_heading_and_body(tmp_path):
    """The previously-working case must keep working."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text(
        "# Program Objective\n\n## Why This Exists\n\nBecause of X.\n"
    )
    out = _write(pcp_dir)
    assert "Because of X." in out.read_text()


def test_objective_falls_back_to_placeholder_when_file_missing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    out = _write(pcp_dir)
    assert "No objective.md found" in out.read_text()


def test_objective_falls_back_to_placeholder_when_only_headings(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    (pcp_dir / "objective.md").write_text("# Program Objective\n\n## Why This Exists\n\n## Out of Scope\n")
    out = _write(pcp_dir)
    assert "No objective.md found" in out.read_text()


def test_objective_truncated_to_500_chars(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    long_text = "x" * 600
    (pcp_dir / "objective.md").write_text(f"# Program Objective\n{long_text}")
    out = _write(pcp_dir)
    md = out.read_text()
    assert "x" * 500 in md
    assert "x" * 501 not in md
