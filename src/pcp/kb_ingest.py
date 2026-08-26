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
