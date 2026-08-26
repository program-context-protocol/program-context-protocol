"""A008 — KB gap detector: flags a topic with zero or thin content against a
deterministic threshold. Zero LLM calls, pure substring match + char count.

A009 — Phase A: given a detected gap, turns raw WebSearch/crawl4ai result
metadata into a staged candidate list with relevance rationale via a
judge-tier LLM call. Never fetches or stores page content.

A010 — Phase B: fetches an already-APPROVED candidate's raw content
verbatim, stores it under .pcp/kb/<topic>/, and re-runs the catalog +
index builders so the new content becomes searchable. Zero LLM calls.
"""

import hashlib
from unittest.mock import patch

import yaml

from pcp.kb_ingest import (
    GAP_THIN,
    GAP_ZERO,
    RESULT_ALREADY_INGESTED,
    RESULT_EMPTY_CONTENT,
    RESULT_FETCH_FAILED,
    RESULT_INGESTED,
    STATUS_APPROVED,
    STATUS_INGESTED,
    STATUS_STAGED,
    THIN_CONTENT_THRESHOLD_CHARS,
    IngestResult,
    StagedCandidate,
    TopicGap,
    detect_content_gaps,
    ingest_approved_candidates,
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


# ── A010: ingest_approved_candidates (Phase B) ──────────────────────────────

def _write_candidate_row(pcp_dir, *, gap_topic_id, url, status, title="Example"):
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / "candidates.yaml"
    existing = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text()) or {}
    rows = existing.get("staged_candidates") or []
    rows.append({
        "gap_topic_id": gap_topic_id,
        "gap_label": gap_topic_id,
        "url": url,
        "title": title,
        "source_kind": "docs",
        "relevance_rationale": "r",
        "discovered_via": "websearch",
        "already_considered": False,
        "status": status,
    })
    existing["staged_candidates"] = rows
    path.write_text(yaml.safe_dump(existing, sort_keys=False))


def _fake_fetcher(content_by_url, calls=None):
    def _fetch(url, timeout):
        if calls is not None:
            calls.append(url)
        return content_by_url[url]
    return _fetch


def test_no_candidates_file_returns_empty(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    with patch("pcp.llm.client.call_json") as mock_call:
        results = ingest_approved_candidates(pcp_dir, fetcher=_fake_fetcher({}))
    assert results == []
    mock_call.assert_not_called()


def test_staged_candidate_not_fetched(tmp_path):
    """Only STATUS_APPROVED candidates are eligible -- a still-staged one
    (Phase A staged it, but no human has approved it yet) is never fetched."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_STAGED,
    )
    calls = []
    results = ingest_approved_candidates(pcp_dir, fetcher=_fake_fetcher({}, calls))
    assert results == []
    assert calls == []
    assert not (pcp_dir / "kb" / "candidate-opa").exists()


def test_rejected_candidate_not_fetched(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status="rejected",
    )
    calls = []
    results = ingest_approved_candidates(pcp_dir, fetcher=_fake_fetcher({}, calls))
    assert results == []
    assert calls == []


def test_approved_candidate_fetched_and_stored_verbatim(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    raw = "# OPA Docs\n\nThis is the raw fetched page content, unmodified.\n"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
        title="OPA docs",
    )
    calls = []
    results = ingest_approved_candidates(
        pcp_dir, fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": raw}, calls),
    )

    assert calls == ["https://openpolicyagent.org/docs"]
    assert len(results) == 1
    r = results[0]
    assert isinstance(r, IngestResult)
    assert r.result == RESULT_INGESTED
    assert r.gap_topic_id == "candidate:opa"
    assert r.topic_dir == "candidate-opa"

    stored = pcp_dir / "kb" / r.topic_dir / r.stored_path.split("/")[-1]
    assert stored.exists()
    # verbatim -- exactly the fetched content, no summarization/paraphrase,
    # no added header/frontmatter mixed into the body.
    assert stored.read_text() == raw
    assert r.content_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_ingest_is_zero_llm_calls(tmp_path):
    """A010 is pure fetch+store+reindex -- the judgment step already
    happened in A009. No LLM call, ever, in this phase."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    with patch("pcp.llm.client.call_json") as mock_call:
        ingest_approved_candidates(
            pcp_dir,
            fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": "content\n"}),
        )
    mock_call.assert_not_called()


def test_ingest_advances_candidate_status_to_ingested(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    ingest_approved_candidates(
        pcp_dir, fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": "content\n"}),
    )
    loaded = load_staged_candidates(pcp_dir)
    assert len(loaded) == 1
    assert loaded[0]["status"] == STATUS_INGESTED


def test_ingest_idempotent_does_not_refetch_on_rerun(tmp_path):
    """State-tracked per topic: a second run over the same (now already-
    ingested) candidate never calls the fetcher again."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    calls = []
    fetcher = _fake_fetcher({"https://openpolicyagent.org/docs": "content\n"}, calls)

    first = ingest_approved_candidates(pcp_dir, fetcher=fetcher)
    second = ingest_approved_candidates(pcp_dir, fetcher=fetcher)

    assert len(first) == 1
    assert first[0].result == RESULT_INGESTED
    assert calls == ["https://openpolicyagent.org/docs"]  # only once, ever
    assert second == []  # status is no longer STATUS_APPROVED -- filtered out


def test_ingest_idempotent_even_if_status_manually_reset_to_approved(tmp_path):
    """Defense in depth: the state file is the authoritative idempotency
    record, not just the candidate row's own status -- even if something
    resets a row back to 'approved', an already-ingested (gap, url) is
    still never re-fetched."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    calls = []
    fetcher = _fake_fetcher({"https://openpolicyagent.org/docs": "content\n"}, calls)
    ingest_approved_candidates(pcp_dir, fetcher=fetcher)

    # simulate a reset back to 'approved' on the same (gap, url)
    candidates_path = pcp_dir / "kb" / "candidates.yaml"
    data = yaml.safe_load(candidates_path.read_text())
    data["staged_candidates"][0]["status"] = STATUS_APPROVED
    candidates_path.write_text(yaml.safe_dump(data, sort_keys=False))

    results = ingest_approved_candidates(pcp_dir, fetcher=fetcher)
    assert calls == ["https://openpolicyagent.org/docs"]  # still only once
    assert len(results) == 1
    assert results[0].result == RESULT_ALREADY_INGESTED


def test_fetch_failure_recorded_not_stored_status_unchanged(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )

    def _boom(url, timeout):
        raise OSError("connection refused")

    results = ingest_approved_candidates(pcp_dir, fetcher=_boom)
    assert len(results) == 1
    assert results[0].result == RESULT_FETCH_FAILED
    assert not (pcp_dir / "kb" / "candidate-opa").exists()
    loaded = load_staged_candidates(pcp_dir)
    assert loaded[0]["status"] == STATUS_APPROVED  # unchanged -- never advanced on failure


def test_empty_fetched_content_not_stored(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    results = ingest_approved_candidates(
        pcp_dir, fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": ""}),
    )
    assert len(results) == 1
    assert results[0].result == RESULT_EMPTY_CONTENT
    assert not (pcp_dir / "kb" / "candidate-opa").exists()


def test_multiple_candidates_land_in_separate_topic_dirs(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    _write_candidate_row(
        pcp_dir, gap_topic_id="dependency:networkx",
        url="https://networkx.org/docs", status=STATUS_APPROVED,
    )
    content = {
        "https://openpolicyagent.org/docs": "opa content\n",
        "https://networkx.org/docs": "networkx content\n",
    }
    results = ingest_approved_candidates(pcp_dir, fetcher=_fake_fetcher(content))
    topic_dirs = {r.topic_dir for r in results}
    assert topic_dirs == {"candidate-opa", "dependency-networkx"}
    assert (pcp_dir / "kb" / "candidate-opa").is_dir()
    assert (pcp_dir / "kb" / "dependency-networkx").is_dir()


def test_ingest_writes_ingest_state_file(tmp_path):
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    ingest_approved_candidates(
        pcp_dir, fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": "content\n"}),
    )
    state_path = pcp_dir / "kb" / "ingest_state.yaml"
    assert state_path.exists()
    state = yaml.safe_load(state_path.read_text())
    assert len(state["ingested"]) == 1
    assert state["ingested"][0]["url"] == "https://openpolicyagent.org/docs"
    assert state["ingested"][0]["gap_topic_id"] == "candidate:opa"


def test_ingest_reruns_catalog_and_index_builders_on_new_content(tmp_path):
    """'Component 3' -- the catalog + index builders -- must actually run
    against the newly-ingested content so it becomes searchable."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )
    ingest_approved_candidates(
        pcp_dir,
        fetcher=_fake_fetcher({"https://openpolicyagent.org/docs": "# Heading\nbody\n"}),
    )

    catalog_path = pcp_dir / "kb" / "catalog" / "candidate-opa.yaml"
    assert catalog_path.exists()
    catalog = yaml.safe_load(catalog_path.read_text())
    assert catalog["heading_count"] == 1

    index_path = pcp_dir / "kb" / "index.yaml"
    assert index_path.exists()


def test_ingest_skips_reindex_when_nothing_new(tmp_path):
    """A no-op run (nothing approved, or everything already ingested) never
    touches the catalog/index -- avoids a pointless full rewrite."""
    pcp_dir = tmp_path / ".pcp"
    pcp_dir.mkdir()
    results = ingest_approved_candidates(pcp_dir, fetcher=_fake_fetcher({}))
    assert results == []
    assert not (pcp_dir / "kb" / "catalog").exists()
    assert not (pcp_dir / "kb" / "index.yaml").exists()


def test_default_fetcher_uses_stdlib_urllib_get(tmp_path):
    """No third-party HTTP dependency -- same posture as uat.py's
    check_url_responds/check_dom_contains (urllib.request, stdlib only)."""
    pcp_dir = tmp_path / ".pcp"
    _write_candidate_row(
        pcp_dir, gap_topic_id="candidate:opa",
        url="https://openpolicyagent.org/docs", status=STATUS_APPROVED,
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"fetched via urllib\n"

    with patch("urllib.request.urlopen", return_value=_FakeResponse()) as mock_open:
        results = ingest_approved_candidates(pcp_dir)  # no fetcher override

    mock_open.assert_called_once()
    assert len(results) == 1
    assert results[0].result == RESULT_INGESTED
    stored = pcp_dir / "kb" / "candidate-opa"
    files = list(stored.glob("*.md"))
    assert len(files) == 1
    assert files[0].read_text() == "fetched via urllib\n"
