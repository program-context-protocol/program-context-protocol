"""A008 — KB gap detector: flags a topic with zero or thin content against a
deterministic threshold. Zero LLM calls, pure substring match + char count.

A009 — Phase A: given a detected gap, turns raw WebSearch/crawl4ai result
metadata into a staged candidate list with relevance rationale via a
judge-tier LLM call. Never fetches or stores page content.
"""

from unittest.mock import patch

from pcp.kb_ingest import (
    GAP_THIN,
    GAP_ZERO,
    STATUS_STAGED,
    THIN_CONTENT_THRESHOLD_CHARS,
    StagedCandidate,
    TopicGap,
    detect_content_gaps,
    load_staged_candidates,
    stage_candidates_for_gap,
    write_staged_candidates,
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


# ── A009: stage_candidates_for_gap ──────────────────────────────────────────

_GAP = TopicGap(
    topic_id="candidate:opa",
    label="opa",
    source="build_vs_buy_candidate",
    gap_kind=GAP_ZERO,
    content_chars=0,
)


def test_no_search_results_skips_llm_call_entirely(tmp_path):
    """Token Discipline: nothing to judge, nothing spent."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.call_json") as mock_call:
        staged = stage_candidates_for_gap(_GAP, [], pcp_dir)
    mock_call.assert_not_called()
    assert staged == []


def test_search_results_all_missing_url_skips_llm_call(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [{"title": "no url here", "snippet": "..."}]
    with patch("pcp.llm.client.call_json") as mock_call:
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    mock_call.assert_not_called()
    assert staged == []


def test_relevant_result_staged_with_rationale(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [
        {"title": "OPA docs", "url": "https://openpolicyagent.org/docs", "snippet": "policy engine"},
    ]
    judge_res = {"candidates": [
        {"index": 0, "source_kind": "docs", "relevance_rationale": "official docs for the gap"},
    ]}
    with patch("pcp.llm.client.call_json", return_value=judge_res) as mock_call:
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir, discovered_via="websearch")

    mock_call.assert_called_once()
    assert len(staged) == 1
    c = staged[0]
    assert isinstance(c, StagedCandidate)
    assert c.gap_topic_id == "candidate:opa"
    assert c.gap_label == "opa"
    assert c.url == "https://openpolicyagent.org/docs"
    assert c.title == "OPA docs"
    assert c.source_kind == "docs"
    assert c.relevance_rationale == "official docs for the gap"
    assert c.discovered_via == "websearch"
    assert c.status == STATUS_STAGED


def test_irrelevant_result_omitted_by_judge_is_not_staged(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [
        {"title": "OPA docs", "url": "https://openpolicyagent.org/docs", "snippet": "policy engine"},
        {"title": "unrelated blog", "url": "https://example.com/other", "snippet": "off topic"},
    ]
    judge_res = {"candidates": [
        {"index": 0, "source_kind": "docs", "relevance_rationale": "relevant"},
    ]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)

    assert len(staged) == 1
    assert staged[0].url == "https://openpolicyagent.org/docs"


def test_judge_call_failure_fails_open_stages_nothing(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [{"title": "x", "url": "https://example.com/x", "snippet": "y"}]
    with patch("pcp.llm.client.call_json", side_effect=RuntimeError("down")):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert staged == []


def test_invalid_source_kind_coerced_to_other(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [{"title": "x", "url": "https://example.com/x", "snippet": "y"}]
    judge_res = {"candidates": [
        {"index": 0, "source_kind": "not_a_real_kind", "relevance_rationale": "still relevant"},
    ]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert len(staged) == 1
    assert staged[0].source_kind == "other"


def test_candidate_with_empty_rationale_is_skipped(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [{"title": "x", "url": "https://example.com/x", "snippet": "y"}]
    judge_res = {"candidates": [{"index": 0, "source_kind": "other", "relevance_rationale": ""}]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert staged == []


def test_candidate_with_out_of_range_index_is_skipped_not_crashed(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = [{"title": "x", "url": "https://example.com/x", "snippet": "y"}]
    judge_res = {"candidates": [{"index": 5, "source_kind": "other", "relevance_rationale": "r"}]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert staged == []


def test_never_passes_extra_content_fields_into_judge_prompt(tmp_path):
    """Defensive boundary: even if a caller's search_results dict carries a
    'content'/'html' field (a misuse of this function's contract, or an
    over-fetching crawl4ai caller), that text never reaches the judge
    prompt or the staged record -- only title/url/snippet do."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    smuggled = "FULL PAGE BODY SHOULD NEVER APPEAR HERE"
    results = [{
        "title": "x", "url": "https://example.com/x", "snippet": "y",
        "content": smuggled, "html": f"<html>{smuggled}</html>",
    }]
    judge_res = {"candidates": [{"index": 0, "source_kind": "other", "relevance_rationale": "r"}]}
    with patch("pcp.llm.client.call_json", return_value=judge_res) as mock_call:
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)

    prompt_text = " ".join(str(a) for a in mock_call.call_args[0])
    assert smuggled not in prompt_text
    assert len(staged) == 1


def test_already_considered_flagged_via_build_vs_buy_candidate_topic(tmp_path):
    """Cross-checks a staged candidate's url/title against topics.yaml's
    build_vs_buy_candidate topics -- kb_topics.py's own
    load_routing_categories docstring: 'a newly-proposed candidate is
    checked against what has already been considered before a human is
    asked to approve fetching it.'"""
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "topics.yaml").write_text(
        "topics:\n"
        "  - id: candidate:opa\n"
        "    source: build_vs_buy_candidate\n"
        "    label: opa\n"
    )
    results = [{"title": "OPA", "url": "https://openpolicyagent.org/docs", "snippet": "s"}]
    judge_res = {"candidates": [{"index": 0, "source_kind": "docs", "relevance_rationale": "r"}]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert len(staged) == 1
    assert staged[0].already_considered is True


def test_not_already_considered_when_no_topic_matches(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "topics.yaml").write_text(
        "topics:\n"
        "  - id: candidate:networkx\n"
        "    source: build_vs_buy_candidate\n"
        "    label: networkx\n"
    )
    results = [{"title": "OPA docs", "url": "https://openpolicyagent.org/docs", "snippet": "s"}]
    judge_res = {"candidates": [{"index": 0, "source_kind": "docs", "relevance_rationale": "r"}]}
    with patch("pcp.llm.client.call_json", return_value=judge_res):
        staged = stage_candidates_for_gap(_GAP, results, pcp_dir)
    assert len(staged) == 1
    assert staged[0].already_considered is False


# ── A009: write_staged_candidates / load_staged_candidates ─────────────────

def test_write_staged_candidates_creates_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    c = StagedCandidate(
        gap_topic_id="candidate:opa", gap_label="opa",
        url="https://openpolicyagent.org/docs", title="OPA docs",
        source_kind="docs", relevance_rationale="r", discovered_via="websearch",
    )
    out = write_staged_candidates(pcp_dir, [c])
    assert out == pcp_dir / "kb" / "candidates.yaml"
    loaded = load_staged_candidates(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["url"] == "https://openpolicyagent.org/docs"
    assert loaded[0]["status"] == STATUS_STAGED


def test_write_staged_candidates_dedupes_by_gap_and_url(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    c1 = StagedCandidate(
        gap_topic_id="candidate:opa", gap_label="opa",
        url="https://openpolicyagent.org/docs", title="OPA docs",
        source_kind="docs", relevance_rationale="first", discovered_via="websearch",
    )
    write_staged_candidates(pcp_dir, [c1])
    c2 = StagedCandidate(
        gap_topic_id="candidate:opa", gap_label="opa",
        url="https://openpolicyagent.org/docs", title="OPA docs",
        source_kind="docs", relevance_rationale="updated rationale", discovered_via="websearch",
    )
    write_staged_candidates(pcp_dir, [c2])

    loaded = load_staged_candidates(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["relevance_rationale"] == "updated rationale"


def test_write_staged_candidates_never_regresses_advanced_status(tmp_path):
    """A human/Phase B already moved this candidate past 'staged' -- a
    re-run of Phase A over overlapping search results must not silently
    reset it back."""
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "candidates.yaml").write_text(
        "staged_candidates:\n"
        "  - gap_topic_id: candidate:opa\n"
        "    gap_label: opa\n"
        "    url: https://openpolicyagent.org/docs\n"
        "    title: OPA docs\n"
        "    source_kind: docs\n"
        "    relevance_rationale: original\n"
        "    discovered_via: websearch\n"
        "    already_considered: false\n"
        "    status: approved\n"
    )
    c = StagedCandidate(
        gap_topic_id="candidate:opa", gap_label="opa",
        url="https://openpolicyagent.org/docs", title="OPA docs",
        source_kind="docs", relevance_rationale="re-staged attempt", discovered_via="websearch",
    )
    write_staged_candidates(pcp_dir, [c])

    loaded = load_staged_candidates(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["status"] == "approved"
    assert loaded[0]["relevance_rationale"] == "original"


def test_write_staged_candidates_preserves_other_gaps_rows(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    c1 = StagedCandidate(
        gap_topic_id="candidate:opa", gap_label="opa",
        url="https://openpolicyagent.org/docs", title="OPA docs",
        source_kind="docs", relevance_rationale="r1", discovered_via="websearch",
    )
    write_staged_candidates(pcp_dir, [c1])
    c2 = StagedCandidate(
        gap_topic_id="dependency:networkx", gap_label="networkx",
        url="https://networkx.org/docs", title="NetworkX docs",
        source_kind="docs", relevance_rationale="r2", discovered_via="crawl4ai",
    )
    write_staged_candidates(pcp_dir, [c2])

    loaded = load_staged_candidates(pcp_dir)
    urls = {row["url"] for row in loaded}
    assert urls == {"https://openpolicyagent.org/docs", "https://networkx.org/docs"}


def test_load_staged_candidates_returns_empty_when_no_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    assert load_staged_candidates(pcp_dir) == []


def test_load_staged_candidates_returns_empty_on_malformed_yaml(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "candidates.yaml").write_text("staged_candidates: [unterminated\n")
    assert load_staged_candidates(pcp_dir) == []
