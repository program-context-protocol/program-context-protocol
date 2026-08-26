"""A008 — KB gap detector: flags a topic with zero or thin content against a
deterministic threshold. Zero LLM calls, pure substring match + char count.
"""

from pcp.kb_ingest import (
    GAP_THIN,
    GAP_ZERO,
    THIN_CONTENT_THRESHOLD_CHARS,
    detect_content_gaps,
)


def _write_topics(pcp_dir, topics):
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    lines = ["topics:"]
    for t in topics:
        lines.append(f"  - id: {t['id']}")
        lines.append(f"    source: {t['source']}")
        lines.append(f"    label: {t['label']}")
    (kb_dir / "topics.yaml").write_text("\n".join(lines) + "\n")


def _write_domain_doc(pcp_dir, name, text):
    domain_dir = pcp_dir / "kb" / "domain"
    domain_dir.mkdir(parents=True, exist_ok=True)
    (domain_dir / name).write_text(text)


def _write_adr(pcp_dir, name, text):
    adr_dir = pcp_dir / "kb" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / name).write_text(text)


# ── no topics.yaml / empty topics ───────────────────────────────────────────

def test_no_topics_file_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert detect_content_gaps(pcp_dir) == []


def test_empty_topics_list_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [])
    assert detect_content_gaps(pcp_dir) == []


# ── zero content ─────────────────────────────────────────────────────────

def test_topic_with_no_matching_content_flagged_zero(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "dependency:networkx", "source": "dependency_manifest", "label": "networkx"},
    ])
    _write_domain_doc(pcp_dir, "general.md", "This file talks about something unrelated entirely.\n")

    gaps = detect_content_gaps(pcp_dir)
    assert len(gaps) == 1
    assert gaps[0].topic_id == "dependency:networkx"
    assert gaps[0].gap_kind == GAP_ZERO
    assert gaps[0].content_chars == 0
    assert gaps[0].matched_files == []


def test_topic_flagged_zero_when_no_kb_content_files_exist_at_all(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
    ])
    gaps = detect_content_gaps(pcp_dir)
    assert len(gaps) == 1
    assert gaps[0].gap_kind == GAP_ZERO


# ── thin content ─────────────────────────────────────────────────────────

def test_topic_with_content_below_threshold_flagged_thin(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
    ])
    # A single short mention -- well under the default threshold.
    _write_domain_doc(pcp_dir, "general.md", "opa is used somewhere.\n")

    gaps = detect_content_gaps(pcp_dir)
    assert len(gaps) == 1
    assert gaps[0].gap_kind == GAP_THIN
    assert 0 < gaps[0].content_chars < THIN_CONTENT_THRESHOLD_CHARS
    assert gaps[0].matched_files == ["general.md"]


def test_thin_threshold_is_configurable_per_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
    ])
    _write_domain_doc(pcp_dir, "general.md", "opa is used somewhere in this project.\n")

    # With a tiny threshold, the same content now clears the bar -- no gap.
    gaps = detect_content_gaps(pcp_dir, threshold_chars=5)
    assert gaps == []


# ── sufficient content: not a gap ───────────────────────────────────────────

def test_topic_with_sufficient_content_not_flagged(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
    ])
    long_text = "opa " + ("is the policy engine this project uses for decision logic. " * 5)
    _write_domain_doc(pcp_dir, "general.md", long_text)

    gaps = detect_content_gaps(pcp_dir)
    assert gaps == []


def test_content_aggregated_across_multiple_files(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
    ])
    chunk = "opa policy decisions are evaluated here in real detail. " * 3
    _write_domain_doc(pcp_dir, "general.md", chunk)
    _write_adr(pcp_dir, "ADR-001-example.md", chunk)

    gaps = detect_content_gaps(pcp_dir)
    assert gaps == []  # combined content from both files clears the threshold


# ── matching is case-insensitive substring, not fuzzy ──────────────────────

def test_matching_is_case_insensitive(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "dependency:networkx", "source": "dependency_manifest", "label": "networkx"},
    ])
    long_text = "NetworkX " + ("handles the coupling graph analysis in this project. " * 5)
    _write_domain_doc(pcp_dir, "general.md", long_text)

    gaps = detect_content_gaps(pcp_dir)
    assert gaps == []


def test_matching_does_not_cross_word_boundaries_incorrectly(tmp_path):
    """Substring match, deliberately simple -- a longer word that CONTAINS
    the label still counts as a mention (documented behavior, not a bug):
    this stays deterministic/zero-LLM rather than adding tokenization."""
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "dependency:click", "source": "dependency_manifest", "label": "click"},
    ])
    long_text = "clicking " + ("through the CLI options is handled by this library extensively. " * 5)
    _write_domain_doc(pcp_dir, "general.md", long_text)

    gaps = detect_content_gaps(pcp_dir)
    assert gaps == []  # "click" matched inside "clicking" -- substring, not word-bounded


# ── topics missing a label are skipped, not crashed on ──────────────────────

def test_topic_without_label_is_skipped(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "topics.yaml").write_text(
        "topics:\n  - id: broken:entry\n    source: dependency_manifest\n"
    )
    gaps = detect_content_gaps(pcp_dir)
    assert gaps == []


def test_malformed_topics_yaml_returns_empty_not_raises(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "topics.yaml").write_text("topics: [unterminated\n")
    assert detect_content_gaps(pcp_dir) == []


# ── multiple topics, mixed outcomes ─────────────────────────────────────────

def test_mixed_topics_only_gaps_returned(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_topics(pcp_dir, [
        {"id": "candidate:opa", "source": "build_vs_buy_candidate", "label": "opa"},
        {"id": "dependency:networkx", "source": "dependency_manifest", "label": "networkx"},
        {"id": "dependency:ghost", "source": "dependency_manifest", "label": "ghostlib"},
    ])
    long_text = "opa " + ("is the policy engine used for decision logic here. " * 5)
    thin_text = "networkx graph.\n"
    _write_domain_doc(pcp_dir, "general.md", long_text + "\n" + thin_text)

    gaps = detect_content_gaps(pcp_dir)
    gap_ids = {g.topic_id: g.gap_kind for g in gaps}
    assert gap_ids == {
        "dependency:networkx": GAP_THIN,
        "dependency:ghost": GAP_ZERO,
    }
    assert "candidate:opa" not in gap_ids
