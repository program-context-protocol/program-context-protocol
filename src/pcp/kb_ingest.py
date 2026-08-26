"""kb_ingest.py — two-phase gap/ingestion engine (kb module).

A008: gap detector. For every topic in `.pcp/kb/topics.yaml` (kb_topics.py's
output — component 2 of the kb module), determines whether the project's
existing kb content (`kb/domain/*.md` + `kb/adr/*.md` prose) says anything
substantive about it, and flags topics with ZERO matching content or THIN
matching content (below a deterministic character-count threshold) as gaps.

Deterministic, zero LLM calls (logic_tier 1): a topic's `label` is matched as
a case-insensitive substring against every line of every kb/domain and
kb/adr file; "content" for a topic is the total character count of every
line that mentions it. Same substring-count approach as narrative_lint's
mechanical half and kb_grounding.py's identifier matching — no
summarisation, no relevance judgment, just a verbatim mention count against
a fixed threshold.

This is the deterministic gate the rest of the two-phase engine sits behind:
Phase A (LLM-driven candidate staging via WebSearch/crawl4ai, never
auto-fetching) only ever runs against topics THIS module flags as gaps —
never against every topic in topics.yaml — and Phase B (deterministic fetch
+ integrity check) is a later criterion again. Detection has to be cheap and
rung-1 precisely because it runs first and gates whether the expensive LLM
research step happens at all.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Below this many characters of matching content, a topic is "thin" rather
# than "covered". Zero matching characters is always a gap regardless of
# this threshold. Deterministic, not tunable at call time by default — a
# human changes this constant via a real code review; `threshold_chars` is
# exposed as a function argument only for callers (tests, future tuning
# work) that need to override it explicitly, never a silent per-call drift.
THIN_CONTENT_THRESHOLD_CHARS = 200

GAP_ZERO = "zero"
GAP_THIN = "thin"

_KB_CONTENT_SUBDIRS = ("domain", "adr")


@dataclass
class TopicGap:
    """One flagged gap: a topic from topics.yaml with zero or thin matching
    content in the project's kb/domain + kb/adr files."""

    topic_id: str
    label: str
    source: str
    gap_kind: str  # GAP_ZERO or GAP_THIN
    content_chars: int
    matched_files: list[str] = field(default_factory=list)


def _load_topics(pcp_dir: Path) -> list[dict]:
    topics_path = Path(pcp_dir) / "kb" / "topics.yaml"
    if not topics_path.exists():
        return []
    try:
        data = yaml.safe_load(topics_path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    topics = data.get("topics")
    return topics if isinstance(topics, list) else []


def _kb_content_files(pcp_dir: Path) -> list[Path]:
    """Every kb/domain/*.md and kb/adr/*.md file — the two human-curated kb
    subdirs that hold real prose (see architect_review.py's own _load_kb,
    which reads the same two locations). topics.yaml itself is never
    scanned as content -- it IS the list being checked, not evidence
    about any one topic."""
    kb_dir = Path(pcp_dir) / "kb"
    files: list[Path] = []
    for sub in _KB_CONTENT_SUBDIRS:
        sub_dir = kb_dir / sub
        if sub_dir.exists():
            files.extend(sorted(p for p in sub_dir.glob("*.md") if p.is_file()))
    return files


def _matching_content(label: str, content_files: list[Path]) -> tuple[int, list[str]]:
    """Case-insensitive substring match of `label` against every line of
    every kb content file. Returns (total matching chars, matched file
    names) -- deterministic, no LLM, no fuzzy/tokenized matching."""
    needle = label.strip().lower()
    if not needle:
        return 0, []

    total_chars = 0
    matched_files: list[str] = []
    for path in content_files:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        file_matched = False
        for line in text.splitlines():
            if needle in line.lower():
                total_chars += len(line.strip())
                file_matched = True
        if file_matched:
            matched_files.append(path.name)
    return total_chars, matched_files


def detect_content_gaps(
    pcp_dir: Path,
    threshold_chars: int = THIN_CONTENT_THRESHOLD_CHARS,
) -> list[TopicGap]:
    """For every topic in `.pcp/kb/topics.yaml`, flags it as a gap when the
    project's kb/domain + kb/adr content says nothing (zero) or too little
    (thin, below `threshold_chars`) about it. Deterministic, zero LLM calls.

    A topic missing a `label` is skipped rather than raised on — defensive
    only, since kb_topics.py's own schema always sets one. Malformed or
    missing topics.yaml degrades to "no topics to check" (empty result)
    rather than crashing, same posture as kb_grounding.load_constraint_index
    for a broken spec.yaml.
    """
    pcp_dir = Path(pcp_dir)
    topics = _load_topics(pcp_dir)
    content_files = _kb_content_files(pcp_dir)

    gaps: list[TopicGap] = []
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        label = topic.get("label")
        if not label:
            continue

        content_chars, matched_files = _matching_content(str(label), content_files)

        if content_chars == 0:
            gap_kind = GAP_ZERO
        elif content_chars < threshold_chars:
            gap_kind = GAP_THIN
        else:
            continue

        gaps.append(TopicGap(
            topic_id=topic.get("id", "?"),
            label=str(label),
            source=topic.get("source", "?"),
            gap_kind=gap_kind,
            content_chars=content_chars,
            matched_files=matched_files,
        ))

    return gaps


# ── A009: Phase A -- LLM-staged candidate identification ───────────────────
#
# Phase A never fetches or stores page content -- that's Phase B (A010), a
# later criterion. This function's job: given raw search-RESULT METADATA
# (title/url/snippet only) for a gap flagged above by detect_content_gaps,
# ask an LLM which of those results are genuinely relevant and why, and
# output a staged candidate list. Nothing here ever downloads a page.
#
# Why this takes `search_results` as an argument instead of calling
# WebSearch/crawl4ai itself: llm/client.py's own harness (`claude -p`, a
# one-shot subprocess) has no tool-use capability, by design -- same
# constraint documented on inspiration_art.py's headless path (CLAUDE.md's
# LLM Routing section: "PCP does not rebuild agentic search infra"). The
# real WebSearch/crawl4ai call happens in whichever session drives this --
# the interactive `/pcp` skill has genuine WebSearch/Agent access, same as
# inspiration_art.py's own research step -- and this function's contract
# starts once those raw results already exist. build_vs_buy for A009
# (acceptance.yaml) records this as reuse_whole of WebSearch/crawl4ai, not a
# rebuild: this module composes their output, it doesn't reimplement search.

SOURCE_KINDS = ("docs", "book", "blog", "bug_tracker", "git_repo", "other")
STATUS_STAGED = "staged"

# A search result's snippet is truncated defensively -- WebSearch/crawl4ai
# both return short result-listing snippets by nature, but this caps it
# regardless so a caller that accidentally passes a full page body (a
# misuse of this function's contract) can't smuggle real fetched content
# into the judge prompt or the staged record under the 'snippet' key.
_SNIPPET_MAX_CHARS = 500

CANDIDATE_STAGING_SYSTEM_PROMPT = (
    "You are staging candidate external sources for a detected knowledge gap in a "
    "project's knowledge base. You are given the gap's label and a numbered list of "
    "search results -- each one ONLY a title, a url, and a short snippet, never full "
    "page content, because none of this has been fetched or verified yet. For each "
    "result that is genuinely relevant to filling this specific gap, classify its "
    "source_kind (exactly one of: docs, book, blog, bug_tracker, git_repo, other) and "
    "write a short relevance_rationale explaining why it is worth fetching later -- "
    "do not claim anything about the page's actual content beyond what the title/"
    "snippet already say. Omit any result that is not genuinely relevant (off-topic, "
    "spam, or a near-duplicate of another result you are keeping). You are staging "
    "candidates for human approval only -- you must not fetch, download, browse, or "
    "otherwise retrieve any of these urls yourself. "
    'Respond with JSON only: {"candidates": [{"index": <int>, "source_kind": "...", '
    '"relevance_rationale": "..."}]}. Empty list if none are relevant.'
)


@dataclass
class StagedCandidate:
    """One Phase-A output row: a candidate external source for a detected
    gap, staged with a relevance rationale -- never fetched, never storing
    any page content. `status` is always STATUS_STAGED coming out of this
    module; Phase B (a later criterion) is the only place a candidate's
    status ever advances past "staged"."""

    gap_topic_id: str
    gap_label: str
    url: str
    title: str
    source_kind: str
    relevance_rationale: str
    discovered_via: str  # "websearch" | "crawl4ai" -- whichever tool found it
    already_considered: bool = False
    status: str = STATUS_STAGED


def _sanitize_search_results(search_results: list[dict]) -> list[dict]:
    """Boundary enforcement for 'never fetched or stored': reads ONLY
    title/url/snippet off each raw result, regardless of what other keys a
    caller's result dict happens to carry (a 'content'/'html'/'body' key,
    for instance, if a crawl4ai call over-fetched upstream of this
    function) -- those never reach the judge prompt or a staged record."""
    sanitized = []
    for r in search_results:
        if not isinstance(r, dict):
            continue
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        sanitized.append({
            "url": url,
            "title": str(r.get("title") or "").strip(),
            "snippet": str(r.get("snippet") or "").strip()[:_SNIPPET_MAX_CHARS],
        })
    return sanitized


def _already_considered_urls_and_titles(pcp_dir: Path) -> set[str]:
    """Lowercased build_vs_buy_candidate labels already on record in
    topics.yaml -- kb_topics.py's own load_routing_categories docstring:
    'a newly-proposed candidate is checked against what has already been
    considered before a human is asked to approve fetching it.' Import is
    lazy to avoid a hard dependency on click/rich (commands/kb_topics.py's
    CLI-layer imports) from this pure-logic module, same posture as
    kb_grounding.py's lazy `from pcp.llm import client as llm`."""
    from pcp.commands.kb_topics import load_routing_categories

    considered: set[str] = set()
    for topic in load_routing_categories(pcp_dir):
        if not isinstance(topic, dict) or topic.get("source") != "build_vs_buy_candidate":
            continue
        label = str(topic.get("label") or "").strip().lower()
        if label:
            considered.add(label)
    return considered


def stage_candidates_for_gap(
    gap: TopicGap,
    search_results: list[dict],
    pcp_dir: Path,
    discovered_via: str = "websearch",
    model: str | None = None,
) -> list[StagedCandidate]:
    """Phase A: turns raw WebSearch/crawl4ai result metadata for one
    detected gap into a staged candidate list with relevance rationale.
    Never fetches or stores page content -- see the section docstring
    above for why this takes `search_results` rather than calling a
    search tool itself, and _sanitize_search_results for the boundary
    that keeps stray fetched content out even if a caller passes it.

    Token Discipline: zero LLM calls when there is nothing to judge (no
    search results, or every result missing a url) -- same posture as
    kb_grounding.check_kb_contradiction's early-return. Fails open (stages
    nothing) on an unreachable/broken judge call, same as
    kb_grounding.check_kb_contradiction's own judge call: an infra failure
    is not evidence any result is relevant, so nothing gets staged rather
    than guessed.
    """
    pcp_dir = Path(pcp_dir)
    sanitized = _sanitize_search_results(search_results)
    if not sanitized:
        return []

    already_considered = _already_considered_urls_and_titles(pcp_dir)

    from pcp.llm import client as llm

    numbered = "\n".join(
        f"[{i}] title={r['title']!r} url={r['url']!r} snippet={r['snippet']!r}"
        for i, r in enumerate(sanitized)
    )
    user_prompt = (
        f"## Detected gap\nlabel: {gap.label!r}\nsource: {gap.source}\n"
        f"gap_kind: {gap.gap_kind}\n\n## Search results\n{numbered}"
    )
    try:
        res = llm.call_json(
            CANDIDATE_STAGING_SYSTEM_PROMPT, user_prompt,
            model=model or llm.JUDGE_MODEL, pcp_dir=pcp_dir,
            command="kb-ingest-stage-candidates",
        )
    except Exception:
        return []

    staged: list[StagedCandidate] = []
    raw_candidates = res.get("candidates") if isinstance(res, dict) else None
    for c in raw_candidates or []:
        if not isinstance(c, dict):
            continue
        idx = c.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < len(sanitized)):
            continue
        rationale = str(c.get("relevance_rationale") or "").strip()
        if not rationale:
            continue
        source_kind = c.get("source_kind")
        if source_kind not in SOURCE_KINDS:
            source_kind = "other"

        result = sanitized[idx]
        haystack = f"{result['url']} {result['title']}".lower()
        staged.append(StagedCandidate(
            gap_topic_id=gap.topic_id,
            gap_label=gap.label,
            url=result["url"],
            title=result["title"],
            source_kind=source_kind,
            relevance_rationale=rationale,
            discovered_via=discovered_via,
            already_considered=any(label in haystack for label in already_considered),
        ))

    return staged


def write_staged_candidates(pcp_dir: Path, candidates: list[StagedCandidate]) -> Path:
    """Appends newly staged candidates to `.pcp/kb/candidates.yaml`, deduped
    by (gap_topic_id, url) so re-running Phase A for the same gap against
    overlapping search results never piles up duplicate rows. A row a human
    or Phase B has already advanced past STATUS_STAGED is left untouched --
    this function only ever adds a new staged row or refreshes the metadata
    of one still at STATUS_STAGED, it never regresses a candidate's status
    backward."""
    pcp_dir = Path(pcp_dir)
    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    out = kb_dir / "candidates.yaml"

    existing: dict = {}
    if out.exists():
        try:
            existing = yaml.safe_load(out.read_text(errors="replace")) or {}
        except yaml.YAMLError:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}

    rows = existing.get("staged_candidates")
    rows = list(rows) if isinstance(rows, list) else []
    index = {
        (r.get("gap_topic_id"), r.get("url")): i
        for i, r in enumerate(rows) if isinstance(r, dict)
    }

    for c in candidates:
        key = (c.gap_topic_id, c.url)
        row = {
            "gap_topic_id": c.gap_topic_id,
            "gap_label": c.gap_label,
            "url": c.url,
            "title": c.title,
            "source_kind": c.source_kind,
            "relevance_rationale": c.relevance_rationale,
            "discovered_via": c.discovered_via,
            "already_considered": c.already_considered,
            "status": STATUS_STAGED,
        }
        if key in index:
            prior = rows[index[key]]
            prior_status = prior.get("status") if isinstance(prior, dict) else None
            if prior_status and prior_status != STATUS_STAGED:
                continue  # advanced past staged already -- never regress it
            rows[index[key]] = row
        else:
            index[key] = len(rows)
            rows.append(row)

    existing["staged_candidates"] = rows
    existing["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.write_text(yaml.safe_dump(existing, sort_keys=False))
    return out


def load_staged_candidates(pcp_dir: Path) -> list[dict]:
    """Pure read of `.pcp/kb/candidates.yaml`'s `staged_candidates` list --
    same posture as kb_topics.load_routing_categories: empty list on a
    missing file, malformed YAML, or a non-list field, never a crash."""
    path = Path(pcp_dir) / "kb" / "candidates.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError:
        return []
    rows = data.get("staged_candidates") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []
