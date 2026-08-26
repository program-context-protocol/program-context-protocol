"""File-metadata card generator -- kb module (A001).

Walks every project source file and writes/updates a per-file grounding
card at the mirrored path under .pcp/kb/file_metadata/<source-path>.yaml.
Coverage is mandatory for every source file (module spec constraint:
"depth is a judgment call, coverage is not") -- authoring real evidence-
tiered claims into a card's `claims` list is a later, separate criterion
(A003); this criterion only guarantees every file gets a card and that an
existing card is updated in place rather than silently skipped.

Deterministic, zero LLM calls (logic_tier 1). Reuses the same source-file
walk pcp scan/pcp import already use (pcp.discovery.scanner) instead of
building a second file inventory -- one definition of "every source file"
for the whole project.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.discovery.scanner import collect_source_files, detect_stack

CARD_VERSION = 1
FILE_METADATA_SUBDIR = "file_metadata"


def file_metadata_root(pcp_dir: Path) -> Path:
    """.pcp/kb/file_metadata/ -- root of the mirrored card tree."""
    return Path(pcp_dir) / "kb" / FILE_METADATA_SUBDIR


def card_path_for(pcp_dir: Path, project_root: Path, source_file: Path) -> Path:
    """Mirrored path for one source file's card:
    .pcp/kb/file_metadata/<source-path-relative-to-project-root>.yaml
    """
    rel = source_file.resolve().relative_to(Path(project_root).resolve())
    return file_metadata_root(pcp_dir) / Path(str(rel) + ".yaml")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_existing_card(card_path: Path) -> dict:
    if not card_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(card_path.read_text())
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_card(project_root: Path, source_file: Path, existing: dict, code_sha: str) -> dict:
    """Compose the card for one source file. Preserves any authored `claims`
    from a prior card (A003's territory) and prior `generated_at` -- this
    criterion updates the card, it never discards accumulated content."""
    rel = str(source_file.resolve().relative_to(Path(project_root).resolve()))
    now = datetime.now(timezone.utc).isoformat()
    card = {
        "card_version": CARD_VERSION,
        "source_path": rel,
        "code_sha_at_verification": code_sha,
        "generated_at": existing.get("generated_at", now),
        "claims": existing.get("claims", []),
    }
    if existing:
        card["updated_at"] = now
    return card


def generate_file_metadata_cards(project_root: Path, pcp_dir: Path) -> dict:
    """Walk every project source file, writing or updating its card. No file
    in the walk is skipped -- every one gets a written or confirmed-current
    card. Returns a summary: {"written": [...], "updated": [...],
    "unchanged": [...], "total": int}, paths relative to project_root."""
    project_root = Path(project_root)
    pcp_dir = Path(pcp_dir)

    stack = detect_stack(project_root)
    source_files = collect_source_files(project_root, stack)

    written, updated, unchanged = [], [], []

    for source_file in source_files:
        rel = str(source_file.relative_to(project_root))
        card_path = card_path_for(pcp_dir, project_root, source_file)
        card_path.parent.mkdir(parents=True, exist_ok=True)

        existing = _load_existing_card(card_path)
        code_sha = _file_sha256(source_file)

        if existing and existing.get("code_sha_at_verification") == code_sha:
            unchanged.append(rel)
            continue

        card = build_card(project_root, source_file, existing, code_sha)
        card_path.write_text(yaml.safe_dump(card, sort_keys=False))
        (updated if existing else written).append(rel)

    return {
        "written": written,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(source_files),
    }
