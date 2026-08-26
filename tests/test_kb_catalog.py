import yaml

from pcp import kb_catalog


def _make_kb(pcp_dir):
    """Two topic directories (adr/, domain/) with real markdown docs, plus a
    non-topic dir (file_metadata/) that must never be treated as a topic."""
    kb_dir = pcp_dir / "kb"
    adr_dir = kb_dir / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-001-example.md").write_text(
        "# ADR-001: Example Decision\n\n"
        "## Status\n\nAccepted.\n\n"
        "## Context\n\nSome context.\n"
    )
    domain_dir = kb_dir / "domain"
    domain_dir.mkdir()
    (domain_dir / "auth.md").write_text(
        "# Auth Domain Knowledge\n\n"
        "## Failure Modes\n\nToken expiry.\n"
    )
    file_metadata_dir = kb_dir / "file_metadata"
    file_metadata_dir.mkdir()
    (file_metadata_dir / "src.py.yaml").write_text("card_version: 1\n")
    return kb_dir


# --- discover_kb_topics ---------------------------------------------------

def test_discover_kb_topics_lists_doc_dirs_excludes_non_doc_dirs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)

    topics = kb_catalog.discover_kb_topics(pcp_dir)

    assert topics == ["adr", "domain"]  # sorted, file_metadata excluded


def test_discover_kb_topics_excludes_catalog_output_dir(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = _make_kb(pcp_dir)
    (kb_dir / "catalog").mkdir()

    topics = kb_catalog.discover_kb_topics(pcp_dir)

    assert "catalog" not in topics
    assert topics == ["adr", "domain"]


def test_discover_kb_topics_missing_kb_dir_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    assert kb_catalog.discover_kb_topics(pcp_dir) == []


# --- extract_headings ------------------------------------------------------

def test_extract_headings_records_line_level_text_verbatim(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Intro text, not a heading.\n"
        "# Top Heading\n"
        "\n"
        "Body.\n"
        "## Second Level  \n"  # trailing whitespace must be stripped
        "###### Deep Heading\n"
    )

    headings = kb_catalog.extract_headings(doc)

    assert headings == [
        {"line": 2, "level": 1, "text": "Top Heading"},
        {"line": 5, "level": 2, "text": "Second Level"},
        {"line": 6, "level": 6, "text": "Deep Heading"},
    ]


def test_extract_headings_missing_file_returns_empty(tmp_path):
    assert kb_catalog.extract_headings(tmp_path / "nope.md") == []


def test_extract_headings_ignores_non_heading_hash_lines(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "Not a heading: # inline hash mid-sentence would still count only "
        "if line starts with #, this line does not.\n"
        "####### seven hashes is not a valid markdown heading level\n"
        "#no-space-after-hash\n"
    )
    assert kb_catalog.extract_headings(doc) == []


# --- walk_topic_docs ---------------------------------------------------

def test_walk_topic_docs_finds_every_markdown_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)

    docs = kb_catalog.walk_topic_docs(pcp_dir, "adr")

    assert [d.name for d in docs] == ["ADR-001-example.md"]


def test_walk_topic_docs_recurses_into_nested_dirs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)
    nested = pcp_dir / "kb" / "domain" / "billing"
    nested.mkdir()
    (nested / "invoices.md").write_text("# Invoices\n")

    docs = kb_catalog.walk_topic_docs(pcp_dir, "domain")

    names = sorted(d.name for d in docs)
    assert names == ["auth.md", "invoices.md"]


def test_walk_topic_docs_missing_topic_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert kb_catalog.walk_topic_docs(pcp_dir, "nope") == []


# --- build_topic_catalog -------------------------------------------------

def test_build_topic_catalog_walks_every_doc_records_headings_verbatim(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)

    catalog = kb_catalog.build_topic_catalog(pcp_dir, "adr")

    assert catalog["topic"] == "adr"
    assert len(catalog["docs"]) == 1
    doc_entry = catalog["docs"][0]
    assert doc_entry["doc"] == "adr/ADR-001-example.md"
    assert doc_entry["headings"] == [
        {"line": 1, "level": 1, "text": "ADR-001: Example Decision"},
        {"line": 3, "level": 2, "text": "Status"},
        {"line": 7, "level": 2, "text": "Context"},
    ]
    assert catalog["heading_count"] == 3


def test_build_topic_catalog_empty_topic_dir_yields_zero_docs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    (kb_dir / "empty_topic").mkdir(parents=True)

    catalog = kb_catalog.build_topic_catalog(pcp_dir, "empty_topic")

    assert catalog == {"topic": "empty_topic", "docs": [], "heading_count": 0}


# --- build_all_catalogs / write_topic_catalogs ----------------------------

def test_build_all_catalogs_one_entry_per_topic(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)

    catalogs = kb_catalog.build_all_catalogs(pcp_dir)

    assert set(catalogs) == {"adr", "domain"}
    assert catalogs["domain"]["heading_count"] == 2


def test_write_topic_catalogs_writes_one_output_file_per_topic(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)

    written = kb_catalog.write_topic_catalogs(pcp_dir)

    written_names = sorted(p.name for p in written)
    assert written_names == ["adr.yaml", "domain.yaml"]
    for path in written:
        assert path.parent == pcp_dir / "kb" / "catalog"

    adr_out = yaml.safe_load((pcp_dir / "kb" / "catalog" / "adr.yaml").read_text())
    assert adr_out["topic"] == "adr"
    assert adr_out["docs"][0]["headings"][0]["text"] == "ADR-001: Example Decision"


def test_write_topic_catalogs_no_topics_writes_nothing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()

    written = kb_catalog.write_topic_catalogs(pcp_dir)

    assert written == []


def test_write_topic_catalogs_rerun_reflects_new_doc(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _make_kb(pcp_dir)
    kb_catalog.write_topic_catalogs(pcp_dir)

    (pcp_dir / "kb" / "adr" / "ADR-002-second.md").write_text("# Second Decision\n")
    kb_catalog.write_topic_catalogs(pcp_dir)

    adr_out = yaml.safe_load((pcp_dir / "kb" / "catalog" / "adr.yaml").read_text())
    assert len(adr_out["docs"]) == 2
    assert adr_out["heading_count"] == 4  # 3 from ADR-001 + 1 from ADR-002


# --- zero-LLM / no-embeddings sanity ---------------------------------------

def test_kb_catalog_is_zero_llm_no_embeddings():
    """No LLM client / embedding library should ever be imported by this
    module -- pure regex + filesystem walk (logic_tier 1)."""
    import inspect
    src = inspect.getsource(kb_catalog)
    assert "llm.client" not in src
    assert "call_json" not in src
    assert "import numpy" not in src
    assert "sentence_transformers" not in src
    assert "openai" not in src.lower()
