"""File-metadata card generator -- kb module (A001, A002, A003).

Walks every project source file and writes/updates a per-file grounding
card at the mirrored path under .pcp/kb/file_metadata/<source-path>.yaml.
Coverage is mandatory for every source file (module spec constraint:
"depth is a judgment call, coverage is not") -- A001 guarantees every
file gets a card and that a stale card gets refreshed rather than
silently skipped.

A002 hardens the card itself into a real schema: `card_version` and
`code_sha_at_verification` are always validated present and well-typed
(`validate_card_schema`, raises `CardSchemaError`), and no card is ever
overwritten in place. When a file changes, its prior card is relocated
byte-for-byte into permanent history (`archive_superseded_card` /
`history_path_for`, under .pcp/kb/file_metadata_history/) BEFORE the
canonical mirrored path receives the new (version-incremented) card, and
that new card is required to carry a non-empty `delta_summary` explaining
what superseded it. The canonical path always holds exactly the current
card; nothing is ever lost, only relocated.

Deterministic, zero LLM calls (logic_tier 1). Reuses the same source-file
walk pcp scan/pcp import already use (pcp.discovery.scanner) instead of
building a second file inventory -- one definition of "every source file"
for the whole project.

A003 -- card content authoring. A card's `claims` list holds statements an
LLM (or a human) makes about a file. The module constraint is absolute:
"A card's tier:cited claim requires a literal quote; a filename or function
name alone is never sufficient evidence -- absence of evidence must be
marked tier:not_grounded explicitly." `author_claim` is the one place a
claim is ever constructed, so there is no path into a card that skips this
discipline; `validate_claims`/`build_card` re-check that same discipline
against claims loaded back off disk, so a hand-edited or corrupted card
can't silently smuggle an unhonest claim into the next generation.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pcp.discovery.scanner import collect_source_files, detect_stack

CARD_VERSION = 1
FILE_METADATA_SUBDIR = "file_metadata"
FILE_METADATA_HISTORY_SUBDIR = "file_metadata_history"


class CardSchemaError(ValueError):
    """Raised when a file-metadata card violates the schema PCP enforces:
    `card_version` and `code_sha_at_verification` must always be present
    and well-typed, and `delta_summary` is required whenever a card
    represents a supersession of a prior card (A002)."""

# The only two honest evidence states a claim can be in. There is no silent
# third state (e.g. an omitted tier) and no partial-credit state that lets a
# claim carry both a tier:not_grounded label and a quote at the same time --
# that would blend "I have real evidence" with "I have none".
VALID_TIERS = frozenset({"cited", "not_grounded"})

# A bare identifier -- a filename ("kb_file_metadata.py") or a function/symbol
# name ("generate_file_metadata_cards") -- has no internal whitespace and is
# built only from path/identifier characters. A literal quote of real prose
# or real code (e.g. "return 1", "def main():") either contains whitespace or
# punctuation outside that set. This is what tells "I quoted the source" apart
# from "I pasted the filename and called it a day".
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_./\\-]+$")


class ClaimValidationError(ValueError):
    """Raised when a claim's evidence tier is not honestly documented --
    a missing/invalid tier, a tier:cited claim with no real quote, a
    tier:cited quote that is just a bare filename/function name, or a
    tier:not_grounded claim smuggling a quote in anyway."""


def file_metadata_root(pcp_dir: Path) -> Path:
    """.pcp/kb/file_metadata/ -- root of the mirrored card tree. Always
    holds exactly the CURRENT card for each source file -- never a
    superseded one (see file_metadata_history_root)."""
    return Path(pcp_dir) / "kb" / FILE_METADATA_SUBDIR


def file_metadata_history_root(pcp_dir: Path) -> Path:
    """.pcp/kb/file_metadata_history/ -- where a superseded card's exact
    prior content is preserved. A card is never overwritten in place:
    before the canonical mirrored path is rewritten, whatever card was
    already there is relocated here first, byte-identical."""
    return Path(pcp_dir) / "kb" / FILE_METADATA_HISTORY_SUBDIR


def card_path_for(pcp_dir: Path, project_root: Path, source_file: Path) -> Path:
    """Mirrored path for one source file's CURRENT card:
    .pcp/kb/file_metadata/<source-path-relative-to-project-root>.yaml
    """
    rel = source_file.resolve().relative_to(Path(project_root).resolve())
    return file_metadata_root(pcp_dir) / Path(str(rel) + ".yaml")


def history_path_for(pcp_dir: Path, project_root: Path, source_file: Path, superseded_version) -> Path:
    """Permanent home for one superseded card generation:
    .pcp/kb/file_metadata_history/<source-path>/v<superseded_version>.yaml
    """
    rel = source_file.resolve().relative_to(Path(project_root).resolve())
    return file_metadata_history_root(pcp_dir) / rel / f"v{superseded_version}.yaml"


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


def validate_card_schema(card: dict, *, is_supersession: bool = False) -> None:
    """Enforce the card schema PCP relies on: `card_version` and
    `code_sha_at_verification` must always be present and well-typed.
    When `is_supersession` is True the card additionally must carry a
    non-empty `delta_summary` explaining what changed -- required, not
    optional, whenever a card replaces a prior one. Raises CardSchemaError
    on any violation; deterministic, no LLM."""
    card_version = card.get("card_version")
    if not isinstance(card_version, int) or isinstance(card_version, bool) or card_version < 1:
        raise CardSchemaError(f"card_version must be a positive int, got {card_version!r}")

    code_sha = card.get("code_sha_at_verification")
    if not isinstance(code_sha, str) or not code_sha:
        raise CardSchemaError(f"code_sha_at_verification must be a non-empty str, got {code_sha!r}")

    if is_supersession:
        delta_summary = card.get("delta_summary")
        if not isinstance(delta_summary, str) or not delta_summary.strip():
            raise CardSchemaError(
                "delta_summary is required and must be a non-empty str when a "
                "card supersedes a prior card"
            )


def build_delta_summary(existing: dict, new_code_sha: str) -> str:
    """Deterministic, human-readable description of what changed between a
    superseded card and its replacement. Required on every supersession
    (validate_card_schema enforces it) -- both sides of the comparison are
    already-known deterministic values, so this stays logic_tier 1, no LLM
    involved."""
    old_version = existing.get("card_version", CARD_VERSION)
    old_sha = existing.get("code_sha_at_verification") or "unknown"
    return f"superseded card_version {old_version} (code_sha {old_sha[:12]} -> {new_code_sha[:12]})"


def archive_superseded_card(pcp_dir: Path, project_root: Path, source_file: Path, existing_card: dict) -> Path:
    """Preserve a superseded card's exact prior bytes before the canonical
    card_path is rewritten. This is what makes an update non-destructive:
    the old card's content is relocated here untouched -- never mutated,
    never discarded -- so the canonical path can safely hold only the
    CURRENT card while every prior generation stays permanently
    retrievable. Idempotent: never re-overwrites a history entry either."""
    superseded_version = existing_card.get("card_version", CARD_VERSION)
    hist_path = history_path_for(pcp_dir, project_root, source_file, superseded_version)
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    if not hist_path.exists():
        hist_path.write_text(yaml.safe_dump(existing_card, sort_keys=False))
    return hist_path


def _looks_like_bare_identifier(value: str) -> bool:
    """True when `value` is nothing but a filename or function/symbol name
    -- a single token of identifier/path characters with no whitespace --
    which never counts as evidence on its own."""
    stripped = value.strip()
    if not stripped:
        return True
    return bool(_BARE_IDENTIFIER_RE.match(stripped))


def author_claim(text: str, tier: str, quote: str | None = None) -> dict:
    """Build one validated claim entry for a card's `claims` list. This is
    the only constructor for a claim -- every claim that ever reaches a
    card, human-authored or LLM-authored, goes through here.

    tier must be exactly "cited" or "not_grounded":
      - "cited" requires a non-empty, non-whitespace `quote` that is real
        quoted text -- a filename or function name alone (module
        constraint) is rejected even if non-empty.
      - "not_grounded" is the explicit admission of absence: no `quote` is
        allowed, so the two evidence states can never blend together.

    Raises ClaimValidationError on any violation.
    """
    if not text or not text.strip():
        raise ClaimValidationError("claim text must not be empty")
    if tier not in VALID_TIERS:
        raise ClaimValidationError(
            f"tier must be one of {sorted(VALID_TIERS)}, got {tier!r}"
        )

    if tier == "cited":
        if not quote or not quote.strip():
            raise ClaimValidationError(
                "tier:cited requires a literal `quote` -- a filename or "
                "function name alone is never sufficient evidence; use "
                "tier:not_grounded if there is no real supporting text"
            )
        if _looks_like_bare_identifier(quote):
            raise ClaimValidationError(
                f"quote {quote!r} is a bare filename/function name, not a "
                "literal quote -- a filename or function name alone never "
                "counts as evidence; use tier:not_grounded instead if there "
                "is no real supporting text"
            )
        return {"text": text.strip(), "tier": "cited", "quote": quote.strip()}

    # tier == "not_grounded"
    if quote:
        raise ClaimValidationError(
            "tier:not_grounded must not carry a `quote` -- that would blend "
            "the two evidence states together"
        )
    return {"text": text.strip(), "tier": "not_grounded"}


def validate_claims(claims: list) -> list[str]:
    """Re-validate a list of claim dicts (e.g. loaded back off a card on
    disk) against the same rules `author_claim` enforces at construction
    time. Returns a list of human-readable problem strings -- empty means
    every claim is honestly tiered. Never raises on malformed input itself
    (a non-dict entry is reported, not a crash)."""
    problems: list[str] = []
    for i, claim in enumerate(claims or []):
        if not isinstance(claim, dict):
            problems.append(f"claim[{i}] is not a mapping: {claim!r}")
            continue
        try:
            author_claim(claim.get("text", ""), claim.get("tier"), claim.get("quote"))
        except ClaimValidationError as exc:
            problems.append(f"claim[{i}] ({claim.get('text', '')!r}): {exc}")
    return problems


def build_card(project_root: Path, source_file: Path, existing: dict, code_sha: str) -> dict:
    """Compose the card for one source file. Preserves any authored `claims`
    from a prior card and prior `generated_at` -- a refresh never discards
    accumulated content. When `existing` is non-empty this is a
    supersession: `card_version` increments and a `delta_summary` is
    required (validate_card_schema enforces both). Preserved claims are
    re-validated (A003) so a hand-edited or corrupted card can't carry an
    unhonestly-tiered claim forward into the next generation; raises
    ClaimValidationError rather than silently dropping or passing through
    a bad claim."""
    rel = str(source_file.resolve().relative_to(Path(project_root).resolve()))
    now = datetime.now(timezone.utc).isoformat()
    is_supersession = bool(existing)
    claims = existing.get("claims", [])
    problems = validate_claims(claims)
    if problems:
        raise ClaimValidationError(
            f"{rel}: existing card has {len(problems)} unhonestly-tiered "
            f"claim(s), refusing to carry forward: " + "; ".join(problems)
        )
    card = {
        "card_version": existing.get("card_version", CARD_VERSION) + 1 if is_supersession else CARD_VERSION,
        "source_path": rel,
        "code_sha_at_verification": code_sha,
        "generated_at": existing.get("generated_at", now),
        "claims": claims,
    }
    if is_supersession:
        card["updated_at"] = now
        card["delta_summary"] = build_delta_summary(existing, code_sha)

    validate_card_schema(card, is_supersession=is_supersession)
    return card


def generate_file_metadata_cards(project_root: Path, pcp_dir: Path) -> dict:
    """Walk every project source file, writing or refreshing its card. No
    file in the walk is skipped -- every one gets a written or confirmed-
    current card. A refresh never overwrites the prior card in place: it is
    archived into history first (see archive_superseded_card), then the
    canonical path gets the new, schema-validated card. Returns a summary:
    {"written": [...], "updated": [...], "unchanged": [...], "total": int},
    paths relative to project_root."""
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

        if existing:
            # Never overwrite the existing card in place: relocate it into
            # permanent history BEFORE the canonical path gets new content.
            archive_superseded_card(pcp_dir, project_root, source_file, existing)

        card_path.write_text(yaml.safe_dump(card, sort_keys=False))
        (updated if existing else written).append(rel)

    return {
        "written": written,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(source_files),
    }
