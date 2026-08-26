"""Heading-TOC catalog builder — kb module (A006).

Walks every markdown doc under each kb "topic" directory (an immediate
subdirectory of .pcp/kb/ that holds authored documentation -- kb/adr/,
kb/domain/, and any future doc bucket placed there), regexes markdown
heading lines, and records each heading verbatim as (line, level, text)
into one output catalog file per topic: .pcp/kb/catalog/<topic>.yaml.

Zero LLM calls, no embeddings -- pure directory walk + regex (logic_tier 1),
a direct port of the same kb/adr + kb/domain walking pattern
architect_review.py's `_load_kb()` already uses (`glob("*.md")` per
subdirectory), generalized here into a reusable, persisted catalog instead
of an ephemeral review-prompt string.

"Topic" here means a documentation-holding subdirectory of .pcp/kb/ --
deliberately NOT the same thing as a `topics.yaml` topic_registry entry
(kb_topics.py, A004). Those two "topic" senses stay distinct on purpose:
topics.yaml's entries are grounding-review flags derived from four
structured, mostly non-markdown inputs (objective headings, build_vs_buy
candidates, dependency-manifest entries, logic_tier rungs) -- most of
those sources aren't prose at all, so there is nothing for a heading-TOC to
extract from them. A kb topic DIRECTORY is the thing that actually holds
markdown docs with real headings worth cataloging. kb/file_metadata/
(per-file grounding cards, A001) and kb/catalog/ (this builder's own
output) are excluded -- neither holds authored markdown docs.
"""

import re
from pathlib import Path

import yaml

# Subdirectories of .pcp/kb/ that are never treated as a doc topic:
# file_metadata/ holds per-file YAML grounding cards (A001), not authored
# prose; catalog/ is this builder's own output directory -- cataloging it
# would make every run redefine its own inputs.
_NON_TOPIC_DIRS = {"file_metadata", "catalog"}

_CATALOG_SUBDIR = "catalog"

# Same heading shape kb_topics.py's _extract_scope_headings already uses
# (ATX headings, level 1-6), but line-anchored per-line so we can also
# capture the line number and level, not just the text.
_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def kb_root(pcp_dir: Path) -> Path:
    """.pcp/kb/ -- root of the knowledge base tree."""
    return Path(pcp_dir) / "kb"


def catalog_root(pcp_dir: Path) -> Path:
    """.pcp/kb/catalog/ -- root of this builder's per-topic output files."""
    return kb_root(pcp_dir) / _CATALOG_SUBDIR


def discover_kb_topics(pcp_dir: Path) -> list[str]:
    """Every immediate subdirectory of .pcp/kb/ that is a doc topic --
    excludes file_metadata/ and catalog/. Sorted for stable, deterministic
    output ordering. Missing kb/ (nothing scaffolded yet) yields []."""
    root = kb_root(pcp_dir)
    if not root.exists():
        return []
    return sorted(
        p.name for p in root.iterdir()
        if p.is_dir() and p.name not in _NON_TOPIC_DIRS
    )


def extract_headings(doc_path: Path) -> list[dict]:
    """Regex-extract every markdown heading line in doc_path, verbatim, in
    file order. Returns [{"line": int, "level": int, "text": str}, ...].
    A missing file yields []. Zero LLM, no embeddings -- pure per-line
    regex, same posture as every other rung-1 check in this module."""
    doc_path = Path(doc_path)
    if not doc_path.exists():
        return []
    entries: list[dict] = []
    for lineno, raw_line in enumerate(
        doc_path.read_text(errors="replace").splitlines(), start=1
    ):
        match = _HEADING_LINE_RE.match(raw_line)
        if not match:
            continue
        entries.append({
            "line": lineno,
            "level": len(match.group(1)),
            "text": match.group(2),
        })
    return entries


def walk_topic_docs(pcp_dir: Path, topic: str) -> list[Path]:
    """Every markdown doc under .pcp/kb/<topic>/, recursive, sorted for
    stable output ordering. A missing topic directory yields []."""
    topic_dir = kb_root(pcp_dir) / topic
    if not topic_dir.exists():
        return []
    return sorted(topic_dir.rglob("*.md"))


def build_topic_catalog(pcp_dir: Path, topic: str) -> dict:
    """One topic's full heading catalog: every doc under kb/<topic>/, each
    doc's headings recorded verbatim as (line, level, text). Doc paths are
    recorded relative to kb/ (e.g. "adr/ADR-001-example.md") so the output
    is portable across machines/project roots."""
    pcp_dir = Path(pcp_dir)
    root = kb_root(pcp_dir)
    docs = []
    for doc_path in walk_topic_docs(pcp_dir, topic):
        docs.append({
            "doc": str(doc_path.relative_to(root)),
            "headings": extract_headings(doc_path),
        })
    return {
        "topic": topic,
        "docs": docs,
        "heading_count": sum(len(d["headings"]) for d in docs),
    }


def build_all_catalogs(pcp_dir: Path) -> dict[str, dict]:
    """topic -> build_topic_catalog(topic), for every discovered kb topic."""
    pcp_dir = Path(pcp_dir)
    return {
        topic: build_topic_catalog(pcp_dir, topic)
        for topic in discover_kb_topics(pcp_dir)
    }


def write_topic_catalogs(pcp_dir: Path) -> list[Path]:
    """Writes one output file per topic: .pcp/kb/catalog/<topic>.yaml.
    Deterministic, zero LLM, no embeddings -- a full re-walk + re-write each
    run (no incremental staleness tracking here; that's file-metadata's own
    concern, A001), so a rerun always reflects the docs on disk right now.
    Returns the list of paths written; no topics yields []."""
    pcp_dir = Path(pcp_dir)
    out_dir = catalog_root(pcp_dir)
    catalogs = build_all_catalogs(pcp_dir)
    if not catalogs:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for topic, data in catalogs.items():
        out_path = out_dir / f"{topic}.yaml"
        out_path.write_text(yaml.safe_dump(data, sort_keys=False))
        written.append(out_path)
    return written
