"""Tests for kb_ingest_integrity.py (A011) -- ingestion integrity gate.

Covers the three deterministic checks (fingerprint, unstripped-HTML,
exact-duplicate-via-hash), the batch orchestrator, the pending/cleared
flag ledger, and the A010 wiring point (gate blocks catalog/index rebuild
while flags are pending, clears once resolved).
"""

import hashlib

import yaml

from pcp.kb_ingest_integrity import (
    FLAG_DUPLICATE,
    FLAG_FINGERPRINT,
    FLAG_STATUS_CLEARED,
    FLAG_STATUS_PENDING,
    FLAG_UNSTRIPPED_HTML,
    IntegrityFlag,
    IntegrityReport,
    check_ingestion_batch,
    clear_integrity_flag,
    detect_fingerprint_flags,
    detect_unstripped_html,
    has_unresolved_flags,
    load_integrity_flags,
    write_integrity_flags,
)

# ── detect_fingerprint_flags ────────────────────────────────────────────────

def test_fingerprint_detects_spa_shell_no_video_found():
    """The real incident this criterion exists to catch: a JS-driven
    video-listing app-shell whose scraped body just says 'No video
    found.' because the scraper couldn't execute the JS."""
    content = "No video found.\n"
    flags = detect_fingerprint_flags(content)
    assert any("no video found" in f for f in flags)


def test_fingerprint_detects_bot_wall_challenge_page():
    content = "Just a moment...\nChecking your browser before accessing example.com.\n"
    flags = detect_fingerprint_flags(content)
    assert flags  # at least one bot-wall signature matched


def test_fingerprint_detects_enable_javascript_shell():
    content = "<noscript>You need to enable JavaScript to run this app.</noscript>"
    flags = detect_fingerprint_flags(content)
    assert flags


def test_fingerprint_case_insensitive():
    content = "ACCESS DENIED"
    flags = detect_fingerprint_flags(content)
    assert flags


def test_fingerprint_no_match_on_real_content():
    content = (
        "# NetworkX Graph Algorithms\n\n"
        "This document explains community detection and coupling scores "
        "using the networkx library's connected_components function.\n"
    )
    assert detect_fingerprint_flags(content) == []


# ── detect_unstripped_html ──────────────────────────────────────────────────

def test_unstripped_html_detects_full_document():
    content = "<!DOCTYPE html><html><head><title>x</title></head><body><p>hi</p></body></html>"
    result = detect_unstripped_html(content)
    assert result is not None
    assert result["tag_count"] > 0


def test_unstripped_html_detects_script_tag():
    content = "<script>window.__DATA__ = {};</script>"
    assert detect_unstripped_html(content) is not None


def test_unstripped_html_no_flag_on_clean_markdown():
    content = (
        "# Heading\n\n"
        "Some prose about generics, e.g. `List<T>` in Java, or a stray "
        "comparison a < b in code.\n"
    )
    assert detect_unstripped_html(content) is None


def test_unstripped_html_no_flag_on_plain_text():
    content = "This is just plain fetched text with no markup at all.\n"
    assert detect_unstripped_html(content) is None


# ── check_ingestion_batch: orchestrator over stored files ──────────────────

def _stored(pcp_dir, topic_dir, filename, content):
    path = pcp_dir / "kb" / topic_dir
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text(content)
    return f"{topic_dir}/{filename}"


def _item(*, gap_topic_id, url, stored_path, content_hash, result="ingested"):
    return {
        "gap_topic_id": gap_topic_id,
        "url": url,
        "result": result,
        "stored_path": stored_path,
        "content_hash": content_hash,
    }


def test_check_ingestion_batch_flags_fingerprint_match(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    content = "No video found.\n"
    stored_path = _stored(pcp_dir, "candidate-opa", "a.md", content)
    h = hashlib.sha256(content.encode()).hexdigest()
    batch = [_item(gap_topic_id="candidate:opa", url="https://x.example/v", stored_path=stored_path, content_hash=h)]

    report = check_ingestion_batch(pcp_dir, batch)
    assert report.checked == 1
    assert any(f.check == FLAG_FINGERPRINT for f in report.flags)


def test_check_ingestion_batch_flags_unstripped_html(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    content = "<html><body><div>raw</div></body></html>"
    stored_path = _stored(pcp_dir, "candidate-opa", "a.md", content)
    h = hashlib.sha256(content.encode()).hexdigest()
    batch = [_item(gap_topic_id="candidate:opa", url="https://x.example/v", stored_path=stored_path, content_hash=h)]

    report = check_ingestion_batch(pcp_dir, batch)
    assert any(f.check == FLAG_UNSTRIPPED_HTML for f in report.flags)


def test_check_ingestion_batch_flags_exact_duplicate_within_batch(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    content = "# Real Doc\n\nSome genuinely unique technical prose here.\n"
    h = hashlib.sha256(content.encode()).hexdigest()
    p1 = _stored(pcp_dir, "candidate-opa", "a.md", content)
    p2 = _stored(pcp_dir, "dependency-networkx", "b.md", content)
    batch = [
        _item(gap_topic_id="candidate:opa", url="https://x.example/1", stored_path=p1, content_hash=h),
        _item(gap_topic_id="dependency:networkx", url="https://x.example/2", stored_path=p2, content_hash=h),
    ]

    report = check_ingestion_batch(pcp_dir, batch)
    dup_flags = [f for f in report.flags if f.check == FLAG_DUPLICATE]
    assert len(dup_flags) == 2  # both sides of the duplicate pair flagged
    flagged_paths = {f.stored_path for f in dup_flags}
    assert flagged_paths == {p1, p2}


def test_check_ingestion_batch_flags_duplicate_against_prior_ingest_state(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    content = "# Already ingested content\n"
    h = hashlib.sha256(content.encode()).hexdigest()
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "ingest_state.yaml").write_text(
        yaml.safe_dump({
            "ingested": [{
                "gap_topic_id": "candidate:opa",
                "url": "https://x.example/old",
                "stored_path": "candidate-opa/old.md",
                "content_hash": h,
            }],
        })
    )
    new_path = _stored(pcp_dir, "dependency-networkx", "new.md", content)
    batch = [_item(gap_topic_id="dependency:networkx", url="https://x.example/new", stored_path=new_path, content_hash=h)]

    report = check_ingestion_batch(pcp_dir, batch)
    dup_flags = [f for f in report.flags if f.check == FLAG_DUPLICATE]
    assert len(dup_flags) == 1
    assert dup_flags[0].stored_path == new_path


def test_check_ingestion_batch_no_flags_on_clean_unique_content(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    content = "# NetworkX Coupling Score\n\nDeterministic graph analysis, no LLM.\n"
    h = hashlib.sha256(content.encode()).hexdigest()
    stored_path = _stored(pcp_dir, "dependency-networkx", "a.md", content)
    batch = [_item(gap_topic_id="dependency:networkx", url="https://x.example/nx", stored_path=stored_path, content_hash=h)]

    report = check_ingestion_batch(pcp_dir, batch)
    assert report.flags == []
    assert report.has_flags is False


def test_check_ingestion_batch_skips_non_ingested_results(tmp_path):
    """A RESULT_ALREADY_INGESTED / RESULT_FETCH_FAILED row has no fresh
    stored content to scan -- must never be dereferenced as a path."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    batch = [_item(gap_topic_id="x", url="https://x.example", stored_path=None, content_hash=None, result="already_ingested")]
    report = check_ingestion_batch(pcp_dir, batch)
    assert report.checked == 0
    assert report.flags == []


# ── flag ledger: write / load / clear ───────────────────────────────────────

def test_write_and_load_integrity_flags(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    flag = IntegrityFlag(
        stored_path="candidate-opa/a.md", gap_topic_id="candidate:opa",
        url="https://x.example", check=FLAG_FINGERPRINT, reason="matched 'no video found'",
    )
    out = write_integrity_flags(pcp_dir, [flag])
    assert out == pcp_dir / "kb" / "integrity_flags.yaml"

    loaded = load_integrity_flags(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["status"] == FLAG_STATUS_PENDING
    assert loaded[0]["stored_path"] == "candidate-opa/a.md"


def test_has_unresolved_flags_true_when_pending(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    write_integrity_flags(pcp_dir, [
        IntegrityFlag(stored_path="a.md", gap_topic_id="x", url="u", check=FLAG_FINGERPRINT, reason="r"),
    ])
    assert has_unresolved_flags(pcp_dir) is True


def test_has_unresolved_flags_false_when_no_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert has_unresolved_flags(pcp_dir) is False


def test_clear_integrity_flag_requires_reason(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    write_integrity_flags(pcp_dir, [
        IntegrityFlag(stored_path="a.md", gap_topic_id="x", url="u", check=FLAG_FINGERPRINT, reason="r"),
    ])
    try:
        clear_integrity_flag(pcp_dir, "a.md", FLAG_FINGERPRINT, "")
        assert False, "expected ValueError for empty reason"
    except ValueError:
        pass


def test_clear_integrity_flag_marks_cleared_and_unblocks(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    write_integrity_flags(pcp_dir, [
        IntegrityFlag(stored_path="a.md", gap_topic_id="x", url="u", check=FLAG_FINGERPRINT, reason="r"),
    ])
    assert has_unresolved_flags(pcp_dir) is True

    cleared = clear_integrity_flag(pcp_dir, "a.md", FLAG_FINGERPRINT, "reviewed: real doc about HTTP 404s")
    assert cleared is True
    assert has_unresolved_flags(pcp_dir) is False

    loaded = load_integrity_flags(pcp_dir)
    assert loaded[0]["status"] == FLAG_STATUS_CLEARED
    assert loaded[0]["cleared_reason"] == "reviewed: real doc about HTTP 404s"


def test_write_integrity_flags_never_regresses_a_cleared_flag(tmp_path):
    """A re-run of check_ingestion_batch over the same stored file (e.g. a
    later ingest run re-scanning) must never silently reopen a flag a human
    already cleared."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    flag = IntegrityFlag(stored_path="a.md", gap_topic_id="x", url="u", check=FLAG_FINGERPRINT, reason="r")
    write_integrity_flags(pcp_dir, [flag])
    clear_integrity_flag(pcp_dir, "a.md", FLAG_FINGERPRINT, "reviewed, false positive")

    write_integrity_flags(pcp_dir, [flag])  # same check fires again on a re-run
    loaded = load_integrity_flags(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["status"] == FLAG_STATUS_CLEARED


# ── IntegrityReport ──────────────────────────────────────────────────────────

def test_integrity_report_has_flags_property():
    assert IntegrityReport(checked=0, flags=[]).has_flags is False
    flag = IntegrityFlag(stored_path="a.md", gap_topic_id="x", url="u", check=FLAG_FINGERPRINT, reason="r")
    assert IntegrityReport(checked=1, flags=[flag]).has_flags is True
