"""Basename cross-reference index builder -- kb module (A007).

Unions two sources of "every file this project knows about" by basename:

1. Carded file_metadata basenames -- every card under
   .pcp/kb/file_metadata/, read for its own `source_path` field (A001's
   card format). This catches files a card still describes even if the
   file itself has since moved or been deleted (the card is stale evidence,
   not proof the file is gone -- surfacing it is the point).
2. A full source-tree walk -- pcp.discovery.scanner's collect_source_files
   over the live tree (the same walk A001 itself uses), which catches any
   file added since the last `pcp kb-file-metadata` run.

For every basename in that union, scans every kb "topic doc" (markdown
files under .pcp/kb/, e.g. kb/adr/*.md, kb/domain/*.md -- the human-curated
narrative layer, distinct from the file_metadata cards and topics.yaml's
generated data) for filename-shaped tokens, and cross-references them back
to the basename set.

Deterministic, zero LLM calls (logic_tier 1) -- pure regex + directory
walk, "direct port of a proven regex/walk approach" per the kb module spec.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.discovery.scanner import collect_source_files, detect_stack
from pcp.kb_file_metadata import FILE_METADATA_SUBDIR

# Filename-shaped token: word/hyphen characters, then a '.', then a short
# alphabetic-led extension. Deliberately permissive (over-matching a doc's
# prose is harmless -- a matched token that isn't actually a known basename
# is simply never cross-referenced, since lookups are basename-set-scoped).
_FILENAME_TOKEN_RE = re.compile(r"[\w][\w\-]*(?:\.[\w\-]+)*\.[A-Za-z][A-Za-z0-9]{0,8}\b")

# Known-generic basenames -- common enough across unrelated modules/projects
# that a bare basename match against one of these should be treated with
# extra caution even when this repo currently has exactly one real file
# under that name (it is the kind of name a future second file, or a doc
# reference from a different module, is likely to collide on).
_GENERIC_STEMS = {
    "__init__", "index", "utils", "util", "config", "settings", "main",
    "app", "test", "tests", "types", "constants", "helpers", "helper",
    "base", "models", "model", "schema", "client", "server", "common",
    "core", "handler", "handlers", "service", "services",
}


def _is_generic_name(basename: str) -> bool:
    stem = Path(basename).stem.lower()
    return stem in _GENERIC_STEMS


def _load_yaml(path: Path) -> dict:
    try:
        loaded = yaml.safe_load(path.read_text(errors="replace"))
    except (yaml.YAMLError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def collect_carded_basenames(pcp_dir: Path) -> dict[str, set[str]]:
    """basename -> set of source_path values, read from every card under
    .pcp/kb/file_metadata/. A card's own `source_path` field is
    authoritative (robust to the card file itself having moved); falls
    back to deriving the path from the card's own location on disk if the
    field is missing or the card is malformed."""
    root = Path(pcp_dir) / "kb" / FILE_METADATA_SUBDIR
    result: dict[str, set[str]] = {}
    if not root.exists():
        return result

    for card_path in sorted(root.rglob("*.yaml")):
        card = _load_yaml(card_path)
        source_path = card.get("source_path")
        if not isinstance(source_path, str) or not source_path:
            rel = str(card_path.relative_to(root))
            source_path = rel.removesuffix(".yaml")
        basename = Path(source_path).name
        if not basename:
            continue
        result.setdefault(basename, set()).add(source_path)
    return result


def collect_walked_basenames(project_root: Path) -> dict[str, set[str]]:
    """basename -> set of relative paths, from a live full source-tree
    walk (same walk pcp.discovery.scanner already provides A001)."""
    project_root = Path(project_root)
    stack = detect_stack(project_root)
    source_files = collect_source_files(project_root, stack)

    result: dict[str, set[str]] = {}
    for source_file in source_files:
        rel = str(source_file.relative_to(project_root))
        basename = source_file.name
        result.setdefault(basename, set()).add(rel)
    return result


def _kb_topic_doc_paths(pcp_dir: Path) -> list[Path]:
    """Every markdown file under .pcp/kb/ EXCEPT the file_metadata card
    tree (data, not prose) -- the human-curated topic docs (kb/adr/*.md,
    kb/domain/*.md, and any sibling doc directories added later)."""
    kb_dir = Path(pcp_dir) / "kb"
    if not kb_dir.exists():
        return []
    metadata_root = kb_dir / FILE_METADATA_SUBDIR
    docs = []
    for path in sorted(kb_dir.rglob("*.md")):
        if metadata_root in path.parents or path == metadata_root:
            continue
        docs.append(path)
    return docs


def collect_kb_references(pcp_dir: Path, known_basenames: set[str]) -> dict[str, list[str]]:
    """basename -> sorted list of kb topic-doc paths (relative to pcp_dir's
    parent) that mention it as a filename-shaped token. Only basenames
    already present in `known_basenames` are recorded -- an incidental
    filename-shaped token in prose that matches nothing real is simply
    never cross-referenced."""
    pcp_dir = Path(pcp_dir)
    project_root = pcp_dir.parent
    references: dict[str, set[str]] = {b: set() for b in known_basenames}

    for doc_path in _kb_topic_doc_paths(pcp_dir):
        text = doc_path.read_text(errors="replace")
        tokens = {Path(m.group(0)).name for m in _FILENAME_TOKEN_RE.finditer(text)}
        if not tokens:
            continue
        try:
            doc_rel = str(doc_path.relative_to(project_root))
        except ValueError:
            doc_rel = str(doc_path)
        for token in tokens:
            if token in references:
                references[token].add(doc_rel)

    return {basename: sorted(paths) for basename, paths in references.items()}


def build_basename_index(project_root: Path, pcp_dir: Path) -> dict:
    """Unions carded file_metadata basenames with a full source-tree walk,
    scans kb topic docs for filename-shaped tokens, and returns the
    per-basename cross-reference index: real_paths, generic_name_caution,
    multi_file_collision, kb_references."""
    project_root = Path(project_root)
    pcp_dir = Path(pcp_dir)

    carded = collect_carded_basenames(pcp_dir)
    walked = collect_walked_basenames(project_root)

    all_basenames = set(carded) | set(walked)
    kb_references = collect_kb_references(pcp_dir, all_basenames)

    index: dict[str, dict] = {}
    collisions = 0
    for basename in sorted(all_basenames):
        real_paths = sorted(carded.get(basename, set()) | walked.get(basename, set()))
        is_collision = len(real_paths) > 1
        if is_collision:
            collisions += 1
        index[basename] = {
            "real_paths": real_paths,
            "multi_file_collision": is_collision,
            "generic_name_caution": _is_generic_name(basename),
            "kb_references": kb_references.get(basename, []),
        }

    return {
        "index": index,
        "counts": {
            "total_basenames": len(index),
            "collisions": collisions,
        },
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_kb_index(pcp_dir: Path, project_root: Path | None = None) -> Path:
    pcp_dir = Path(pcp_dir)
    project_root = Path(project_root) if project_root is not None else pcp_dir.parent
    data = build_basename_index(project_root, pcp_dir)

    kb_dir = pcp_dir / "kb"
    kb_dir.mkdir(parents=True, exist_ok=True)
    out = kb_dir / "index.yaml"
    out.write_text(yaml.safe_dump(data, sort_keys=False))
    return out
